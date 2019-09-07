from torch.utils.data import Dataset
import torch
import numpy as np
import pandas as pd
from scipy import ndimage

class XYData(Dataset): # torch.utils.data.Dataset
    def __init__(self, dataset_dir, Y_cols, train=True, transform=None, interpolation=224):
        self.dataset_dir = dataset_dir
        self.transform = transform
        self.target_transform = torch.Tensor
        self.Y_cols = Y_cols
        self.train = train
        #self.df = pd.read_csv('../input/clean-full-train/clean_full_data.csv') #+ '/clean_full_data.csv')

        if self.train:
            self.df = pd.read_csv(self.dataset_dir + '/metadata.csv')
        else:
            self.df = pd.read_csv(self.dataset_dir + '/metadata.csv')

        self.interpolation = interpolation
        self.n_data = self.df.shape[0]

    def __getitem__(self, index):
        img_path = self.df.iloc[index]['img_path']
        img = np.load(img_path)
        img = ndimage.zoom(img, self.interpolation/100, order=1) # TODO: consider order=3
        img = np.stack([img, img, img], axis=0).astype(np.float32)

        Y_row = self.df.iloc[index][self.Y_cols].values.astype(np.float32)

        return img, Y_row

    def __len__(self):
        return self.n_data

if __name__ == "__main__":
    from torch.utils.data import DataLoader
    import torch

    data = XYData('data/tdlmc_train_DiagonalBNNPrior_seed1113',
                  Y_cols=['lens_mass_theta_E', 'lens_mass_gamma'],
                  train=True, transform=None, interpolation=150)
    loader = DataLoader(data, batch_size=20, shuffle=False)

    for batch_idx, xy_batch in enumerate(loader):
        if xy_batch[0].shape[0] != 20:
            print(len(xy_batch)) # should be 2, x and y
            print(xy_batch[0].shape)
            print(xy_batch[1].shape)