import numpy as np
import matplotlib.pyplot as plt

from models.encoder_decoder import VQVAEEncoder, VQVAEDecoder
from models.vq import VectorQuantize

from utils import (compute_downsample_rate,

                        time_to_timefreq,
                        timefreq_to_time,
                        quantize,
                        )

from models.base_model import BaseModel, detach_the_unnecessary
from supervised_FCN.example_pretrained_model_loading import load_pretrained_FCN

import pytorch_lightning as pl
import torch
import torch.nn.functional as F
from torch.optim.lr_scheduler import CosineAnnealingLR
import umap
import wandb
from experiments.tests import svm_test, knn_test, svm_test_gs_rbf, intristic_dimension, calculate_entropy, minmax_scale, kmeans_clustering_silhouette
from sklearn.decomposition import PCA
from models.barlowtwins import BarlowTwins, Projector

from umap import UMAP

class BarlowTwinsVQVAE(BaseModel):
    #requires the augmented batches.
    def __init__(self,
                 input_length,
                 non_aug_test_data_loader,
                 non_aug_train_data_loader,
                 config: dict,
                 n_train_samples: int,
                ):
        super().__init__()

        self.config = config
        self.T_max = config['trainer_params']['max_epochs']['vqvae'] * (np.ceil(n_train_samples / config['dataset']['batch_sizes']['vqvae']) + 1)
        
        self.n_fft = config['VQVAE']['n_fft']
        dim = config['encoder']['dim']
        in_channels = config['dataset']['in_channels']

        downsampled_width = config['encoder']['downsampled_width']
        downsampled_rate = compute_downsample_rate(input_length, self.n_fft, downsampled_width)

        #encoder
        self.encoder = VQVAEEncoder(dim, 2*in_channels, downsampled_rate, config['encoder']['n_resnet_blocks'], config['encoder']['dropout_rate'])
        
        #vector quantiser
        self.vq_model = VectorQuantize(dim, config['VQVAE']['codebook']['size'], **config['VQVAE'])

        #decoder
        self.decoder = VQVAEDecoder(dim, 2 * in_channels, downsampled_rate, config['decoder']['n_resnet_blocks'], config['decoder']['dropout_rate'])

        #projector
        projector = Projector(last_channels_enc=dim, proj_hid=config['barlow_twins']['proj_hid'], proj_out=config['barlow_twins']['proj_out'], 
                              device=torch.device("cuda" if torch.cuda.is_available() else "cpu"))
        
        #barlow twins loss function
        self.barlow_twins = BarlowTwins(projector, lambda_=0.005)
        self.beta_barlow = self.config['barlow_twins']['gamma']

        #save these for representation tests during training
        self.train_data_loader = non_aug_train_data_loader
        self.test_data_loader = non_aug_test_data_loader

    def forward(self, batch, training=True):      
        #logic in case validation step
        if training:
            subxs_pair, y = batch
            x_view1, x_view2 = subxs_pair
        else:
            x_view1, y = batch
            x_view2 = None
    

        recons_loss = {'time': 0., 'timefreq': 0., 'perceptual': 0.}
        vq_losses = None
        perplexity = 0.

        #--- forward view 1 ---
        #STFT
        C = x_view1.shape[1]
        u1 = time_to_timefreq(x_view1, self.n_fft, C)

        if not self.decoder.is_upsample_size_updated:
            self.decoder.register_upsample_size(torch.IntTensor(np.array(u1.shape[2:])))

        #encode
        z1 = self.encoder(u1)
        #vector quantize
        z_q1, indices1, vq_loss1, perp1 = quantize(z1, self.vq_model)
        #use view 1 in case view 2 is None
        use_view1 = True

        if x_view2 is not None:
            # --- forward view 2 ---
            #STFT
            u2 = time_to_timefreq(x_view2, self.n_fft, C)
            #encode
            z2 = self.encoder(u2)
            #vector quantize
            z_q2, indices2, vq_loss2, perp2 = quantize(z2, self.vq_model)

            #calculate barlow twins loss
            barlow_twins_loss = self.barlow_twins(z1, z2)
            
            use_view1 = np.random.rand() < 0.5
        else:
            barlow_twins_loss = torch.tensor(0.0)

        
        #reconstruct:
        uhat = self.decoder(z_q1 if use_view1 else z_q2)
        xhat = timefreq_to_time(uhat, self.n_fft, C, original_length=x_view1.size(2) if use_view1 else x_view2.size(2))
        target_x, target_u = (x_view1, u1) if use_view1 else (x_view2, u2)

        #loss
        recons_loss['time'] = F.mse_loss(target_x, xhat)
        recons_loss['timefreq'] = F.mse_loss(target_u, uhat)

        vq_loss = vq_loss1 if use_view1 else vq_loss2
        perplexity = perp1 if use_view1 else perp2


        #calculate entropy
        entropy = calculate_entropy(indices1 if use_view1 else indices2, self.vq_model)

        # plot `x` and `xhat`
        r = np.random.uniform(0, 1)

        if r < 0.01 and training:
            b = np.random.randint(0, target_x.shape[0])
            c = np.random.randint(0, target_x.shape[1])
            fig, ax = plt.subplots()
            plt.suptitle(f'ep_{self.current_epoch}')
            
            label1 = "view1-target" if use_view1 else "view1"
            label2 = "view2-target" if not use_view1 else "view2"
            label3 = "view1 - reconstruction" if use_view1 else "view2 - reconstruction"
            alpha1 = 1 if use_view1 else 0.2
            alpha2 = 1 if not use_view1 else 0.2

            ax.plot(x_view1[b, c].cpu(), label=f"{label1}", c="gray", alpha=alpha1)
            ax.plot(x_view2[b, c].cpu(), label=f"{label2}", c="gray", alpha=alpha2)
            ax.plot(xhat[b,c].detach().cpu(), label=f"{label3}")
            ax.set_title('x')
            ax.set_ylim(-5, 5)
            fig.legend()
            wandb.log({"Reconstruction": wandb.Image(plt)})
            plt.close()
            
        return recons_loss, vq_loss, perplexity, barlow_twins_loss, entropy
    
        
    def training_step(self, batch, batch_idx):
        x = batch
        #forward:
        recons_loss, vq_loss, perplexity, barlow_twins_loss, entropy = self.forward(x)

        #calculate vqvae loss:
        vqvae_loss = recons_loss['time'] + recons_loss['timefreq'] + vq_loss['loss'] + recons_loss['perceptual'] 

        #total loss:
        loss = vqvae_loss + self.beta_barlow * barlow_twins_loss

        # lr scheduler
        sch = self.lr_schedulers()
        sch.step()

        # log
        loss_hist = {'loss': loss,
                     'recons_loss.time': recons_loss['time'],

                     'recons_loss.timefreq': recons_loss['timefreq'],

                     'commit_loss': vq_loss['commit_loss'],
                     #'commit_loss': vq_loss, #?
                     
                     'perplexity': perplexity,

                     'perceptual': recons_loss['perceptual'],

                     'barlow_twins_loss': barlow_twins_loss,

                     'entropy': entropy,
                     }
        
        wandb.log(loss_hist)

        detach_the_unnecessary(loss_hist)
        return loss_hist
    
    def validation_step(self, batch, batch_idx):
        x = batch
        recons_loss, vq_loss, perplexity, barlow_twins_loss, entropy = self.forward(x, training=False)

        #only vqvae
        loss = recons_loss['time'] + recons_loss['timefreq'] + vq_loss['loss'] + recons_loss['perceptual']

        # log
        val_loss_hist = {'validation_loss': loss,
                     'validation_recons_loss.time': recons_loss['time'],

                     'validation_recons_loss.timefreq': recons_loss['timefreq'],

                     'validation_commit_loss': vq_loss['commit_loss'],
                     #'validation_commit_loss': vq_loss, #?
                     
                     'validation_perplexity': perplexity,

                     'validation_perceptual': recons_loss['perceptual'],

                     'validation_entropy': entropy

                     }
        
        detach_the_unnecessary(val_loss_hist)
        wandb.log(val_loss_hist)

        return val_loss_hist


    def configure_optimizers(self):
        opt = torch.optim.AdamW([{'params': self.encoder.parameters(), 'lr': self.config['model_params']['LR']},
                                 {'params': self.decoder.parameters(), 'lr': self.config['model_params']['LR']},
                                 {'params': self.vq_model.parameters(), 'lr': self.config['model_params']['LR']},
                                 ],
                                weight_decay=self.config['model_params']['weight_decay'])
        
        return {'optimizer': opt, 'lr_scheduler': CosineAnnealingLR(opt, self.T_max)}


    def test_step(self, batch, batch_idx):
        x = batch
        recons_loss, vq_loss, perplexity, barrow_twins_loss, _ = self.forward(x)

        loss = recons_loss['time'] + recons_loss['timefreq'] + vq_loss['loss'] + recons_loss['perceptual']
        
        # log
        loss_hist = {'loss': loss,
                     'recons_loss.time': recons_loss['time'],

                     'recons_loss.timefreq': recons_loss['timefreq'],

                     'commit_loss': vq_loss['commit_loss'],
                     #'commit_loss': vq_loss, #?
                     
                     'perplexity': perplexity,

                     'perceptual': recons_loss['perceptual'],

                     'barrow_twins_loss': barrow_twins_loss
                     }
        
        detach_the_unnecessary(loss_hist)
        return loss_hist
    
    # ---- Representation testing ------
    def on_train_epoch_end(self):
        tested = False
        if self.current_epoch % 300 == 0 and self.current_epoch != 0:
            self.test_representations()
            tested = True

        if self.current_epoch == self.config['trainer_params']['max_epochs']['barlowvqvae']-1 and tested == False:
            self.test_representations()

    def on_train_epoch_start(self):
        if self.current_epoch == 0:
            self.test_representations()


    def test_representations(self):
        print("Grabbing discrete latent variables")
        ztr, ytr = self.encode_data(self.train_data_loader, self.encoder, self.vq_model)
        zts, yts = self.encode_data(self.test_data_loader, self.encoder, self.vq_model)    

        ztr = torch.flatten(ztr, start_dim=1).detach().cpu().numpy()
        zts = torch.flatten(zts, start_dim=1).detach().cpu().numpy()
        ytr = torch.flatten(ytr, start_dim=0).detach().cpu().numpy()
        yts = torch.flatten(yts, start_dim=0).detach().cpu().numpy()

        ztr, zts = minmax_scale(ztr, zts)

        z = np.concatenate((ztr, zts), axis=0)
        y = np.concatenate((ytr, yts), axis=0)
        
        intristic_dim = intristic_dimension(z.reshape(-1, z.shape[-1]))
        svm_acc = svm_test(ztr, zts, ytr, yts)
        print("calculating silhuettes..")
        silhuettes = kmeans_clustering_silhouette(z, y, n_runs=15)
        sil_mean, sil_std = np.mean(silhuettes), np.std(silhuettes)
        knn1_acc, knn5_acc, knn10_acc = knn_test(ztr, zts, ytr, yts)

        wandb.log({
            'intrinstic_dim': intristic_dim,
            'svm_acc': svm_acc,
            'sil_mean': sil_mean,
            'sil_std': sil_std,
            #'svm_rbf': svm_gs_rbf_acc,
            'knn1_acc': knn1_acc,
            'knn5_acc': knn5_acc,
            'knn10_acc': knn10_acc,
            #'km_nmi_mean': km_nmi_mean,
            #'km_nmi_std': km_nmi_std
        })

        f, a = plt.subplots(figsize=(6, 6))
        a.boxplot(silhuettes)
        plt.title('Box plot of silhouette scores [BT-VQVAE]')
        plt.ylabel('Silhouette Score')
        plt.xticks([1], ['Clusters'])
        wandb.log({"Sil Boxplot": wandb.Image(f)})
        plt.close()
        
        embs = PCA(n_components=2).fit_transform(z)
        f, a = plt.subplots()
        plt.suptitle(f'ep_{self.current_epoch}')
        a.scatter(embs[:, 0], embs[:, 1], c=y, s=3)
        wandb.log({"PCA plot": wandb.Image(f)})
        plt.close()
        
        embs_u = umap.UMAP(init='spectral').fit_transform(z)
        f, a = plt.subplots(figsize=(8, 8))
        plt.suptitle(f'ep_{self.current_epoch}')
        a.scatter(embs_u[:, 0], embs_u[:, 1], c=y, s=3)
        wandb.log({"UMAP plot": wandb.Image(f)})
        plt.close()


    def encode_data(self, dataloader, encoder, vq_model = None, cuda=True):
        z_list = []  # List to hold all the encoded representations
        y_list = []  # List to hold all the labels/targets

        # Iterate over the entire dataloader
        for batch in dataloader:
            x, y = batch  # Unpack the batch.

            # Perform the encoding
            if cuda:
                x = x.cuda()
            C = x.shape[1]
            xf = time_to_timefreq(x, self.n_fft, C).to(x.device)  # Convert time domain to frequency domain
            z = encoder(xf)  # Encode the input

            if vq_model is not None:
                z, _, _, _ = quantize(z, vq_model)
            # Convert the tensors to lists and append to z_list and y_list
            z_list.extend(z.cpu().detach().tolist())
            y_list.extend(y.cpu().detach().tolist())  # Make sure to detach y and move to CPU as well

        # Convert lists of lists to 2D tensors
        z_encoded = torch.tensor(z_list)
        ys = torch.tensor(y_list)
        if cuda:
            z_encoded = z_encoded.cuda()
            ys = ys.cuda()

        return z_encoded, ys
