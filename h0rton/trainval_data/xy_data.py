import os
import numpy as np
import pandas as pd
import astropy.io.fits as pyfits
import torch
from torch.utils.data import Dataset
import torchvision.transforms as transforms
from baobab.data_augmentation.noise_torch import NoiseModelTorch
from baobab.sim_utils import add_g1g2_columns
from .data_utils import rescale_01, whiten_Y_cols, plus_1_log

__all__ = ['XYData', 'XData',]

class XYData(Dataset): # torch.utils.data.Dataset
    """Represents the XYData used to train or validate the BNN

    """
    def __init__(self, dataset_dir, data_cfg):
        """
        Parameters
        ----------
        dataset_dir : str or os.path object
            path to the directory containing the images and metadata
        data_cfg : dict or Dict
            copy of the `data` field of `BNNConfig`

        """
        self.__dict__ = data_cfg
        self.dataset_dir = dataset_dir
        # Rescale pixels, stack filters, and shift/scale pixels on the fly 
        rescale = transforms.Lambda(rescale_01)
        log = transforms.Lambda(plus_1_log)
        transforms_list = []
        if self.log_pixels:
            transforms_list.append(log)
        if self.rescale_pixels:
            transforms_list.append(rescale)
        if len(transforms_list) == 0:
            self.X_transform = None
        else:
            self.X_transform = transforms.Compose(transforms_list)
        # Y metadata
        metadata_path = os.path.join(self.dataset_dir, 'metadata.csv')
        Y_df = pd.read_csv(metadata_path, index_col=False)
        Y_df = add_g1g2_columns(Y_df)
        # Define source light position as offset from lens mass
        if self.define_src_pos_wrt_lens:
            Y_df['src_light_center_x'] -= Y_df['lens_mass_center_x']
            Y_df['src_light_center_y'] -= Y_df['lens_mass_center_y']
        # Take only the columns we need
        self.Y_df = Y_df[self.Y_cols + ['img_filename']].copy()
        # Size of dataset
        self.n_data = self.Y_df.shape[0]
        # Number of predictive columns
        self.Y_dim = len(self.Y_cols)
        # Log parameterizing
        self.Y_df = whiten_Y_cols(self.Y_df, self.train_Y_mean, self.train_Y_std, self.Y_cols)
        # Adjust exposure time relative to that used to generate the noiseless images
        self.exposure_time_factor = self.noise_kwargs.exposure_time/self.noiseless_exposure_time
        if self.add_noise:
            self.noise_model = NoiseModelTorch(**self.noise_kwargs)

    def __getitem__(self, index):
        # Image X
        img_filename = self.Y_df.iloc[index]['img_filename']
        img_path = os.path.join(self.dataset_dir, img_filename)
        img = np.load(img_path)
        img *= self.exposure_time_factor
        img = torch.as_tensor(img.astype(np.float32)) # np array type must match with default tensor type
        if self.add_noise:
            img += self.noise_model.get_noise_map(img)
        img = self.X_transform(img).unsqueeze(0)
        # Label Y
        Y_row = self.Y_df.iloc[index][self.Y_cols].values.astype(np.float32)
        Y_row = torch.as_tensor(Y_row)
        return img, Y_row

    def __len__(self):
        return self.n_data

class XData(Dataset): # torch.utils.data.Dataset
    """Represents the XData used to test the BNN

    """
    def __init__(self, img_paths, data_cfg):
        """
        Parameters
        ----------
        img_paths : list
            list of image paths. Indexing is based on order in this list.
        data_cfg : dict or Dict
            copy of the `data` field of `BNNConfig`

        """
        self.__dict__ = data_cfg
        self.img_paths = img_paths
        self.X_transform = torch.Tensor

    def __getitem__(self, index):
        hdul = pyfits.open(self.img_paths[index])
        img = hdul['PRIMARY'].data
        img = np.stack([img]*self.n_filters, axis=0).astype(np.float32)
        # Transformations
        img = self.X_transform(img)

        return img

    def __len__(self):
        return self.n_data