import pandas as pd
import numpy as np
from torch.utils.data import Dataset, DataLoader
from sklearn.preprocessing import LabelEncoder
import math
import os

"""
Code taken from:
    https://github.com/ML4ITS/TimeVQVAE/blob/main/preprocessing/preprocess_ucr.py
"""


class UCRDatasetImporter(object):
    def __init__(self,
                dataset_name: str,
                data_scaling: bool,
                **kwargs):
        
        #source: https://github.com/ML4ITS/TimeVQVAE/blob/main/preprocessing/preprocess_ucr.py
        
        self.root_dir = '../data/UCRArchive_2018/'
        
        assert os.path.exists(self.root_dir), 'UCRArchive_2018 does not exist.. please add it to the data folder'

        df_train = pd.read_csv(self.root_dir + f'{dataset_name}/' +f'/{dataset_name}_TRAIN.tsv', sep='\t', header=None)
        df_test = pd.read_csv(self.root_dir + f'{dataset_name}/' + f'/{dataset_name}_TEST.tsv', sep='\t', header=None)

        self.X_train, self.X_test = df_train.iloc[:, 1:].values, df_test.iloc[:, 1:].values
        self.Y_train, self.Y_test = df_train.iloc[:, [0]].values, df_test.iloc[:, [0]].values

        le = LabelEncoder()
        self.Y_train = le.fit_transform(self.Y_train.ravel())[:, None]
        self.Y_test = le.transform(self.Y_test.ravel())[:, None]

        if data_scaling:
            # following [https://github.com/White-Link/UnsupervisedScalableRepresentationLearningTimeSeries/blob/dcc674541a94ca8a54fbb5503bb75a297a5231cb/ucr.py#L30]
            mean = np.nanmean(self.X_train)
            var = np.nanvar(self.X_train)
            self.X_train = (self.X_train - mean) / math.sqrt(var)
            self.X_test = (self.X_test - mean) / math.sqrt(var)

        np.nan_to_num(self.X_train, copy=False)
        np.nan_to_num(self.X_test, copy=False)
        
        print('self.X_train.shape:', self.X_train.shape)
        print('self.X_test.shape:', self.X_test.shape)

        print("# unique labels (train):", np.unique(self.Y_train.reshape(-1)))
        print("# unique labels (test):", np.unique(self.Y_test.reshape(-1)))


class UCRDataset(Dataset):
    def __init__(self, kind: str, dataset_importer: UCRDatasetImporter, **kwargs):
        super().__init__()
        self.kind = kind

        if kind == 'train':
            self.X, self.Y = dataset_importer.X_train, dataset_importer.Y_train
        elif kind == 'test':
            self.X, self.Y = dataset_importer.X_test, dataset_importer.Y_test
        else:
            raise ValueError
        
        self._len = self.X.shape[0]


    @staticmethod
    def _assign_float32(*xs):
        """
        assigns `dtype` of `float32`
        so that we wouldn't have to change `dtype` later before propagating data through a model.
        """
        new_xs = []
        for x in xs:
            new_xs.append(x.astype(np.float32))
        return new_xs[0] if (len(xs) == 1) else new_xs

    def getitem_default(self, idx):
        x, y = self.X[idx, :], self.Y[idx, :]
        x = x[None, :]  # adds a channel dim
        return x, y

    def __getitem__(self, idx):
        return self.getitem_default(idx)

    def __len__(self):
        return self._len