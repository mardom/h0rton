from abc import ABC, abstractmethod
import random
import numpy as np
import torch
__all__ = ['BaseGaussianBNNPosterior', 'DiagonalGaussianBNNPosterior', 'LowRankGaussianBNNPosterior', 'DoubleGaussianBNNPosterior']

class BaseGaussianBNNPosterior(ABC):
    """Abstract base class to represent the Gaussian BNN posterior

    Gaussian posteriors or mixtures thereof with various forms of the covariance matrix inherit from this class.

    """
    def __init__(self, Y_dim, device, whitened_Y_cols_idx=None, Y_mean=None, Y_std=None, log_parameterized_Y_cols_idx=None):
        """
        Parameters
        ----------
        pred : torch.Tensor of shape `[1, out_dim]` or `[out_dim,]`
            raw network output for the predictions
        Y_dim : int
            number of parameters to predict
        whitened_Y_cols_idx : list
            list of Y_cols indices that were whitened
        Y_mean : list
            mean values for the original values of `whitened_Y_cols`
        Y_std : list
            std values for the original values of `whitened_Y_cols`
        log_parameterized_Y_cols_idx : list
            list of Y_cols indices that were log-parameterized
        device : torch.device object

        """
        self.Y_dim = Y_dim
        self.whitened_Y_cols_idx = whitened_Y_cols_idx
        self.Y_mean = Y_mean
        self.Y_std = Y_std
        self.log_parameterized_Y_cols_idx = log_parameterized_Y_cols_idx
        if len(self.whitened_Y_cols_idx) == 0 or self.whitened_Y_cols_idx is None:
            self.whitened_Y_cols_idx = None
            self.Y_mean = None
            self.Y_std = None
        if len(self.log_parameterized_Y_cols_idx) == 0 or self.log_parameterized_Y_cols_idx is None:
            self.log_parameterized_Y_cols_idx = None
        self.device = device
        self.sigmoid = torch.nn.Sigmoid()
        self.logsigmoid = torch.nn.LogSigmoid()

    def seed_samples(self, sample_seed):
        """Seed the sampling for reproducibility

        Parameters
        ----------
        sample_seed : int

        """
        np.random.seed(sample_seed)
        random.seed(sample_seed)
        torch.manual_seed(sample_seed)
        torch.cuda.manual_seed(sample_seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    @abstractmethod
    def sample(self, n_samples, sample_seed=None):
        """Sample from the Gaussian posterior. Must be overridden by subclasses.

        Parameters
        ----------
        n_samples : int
            how many samples to obtain
        sample_seed : int
            seed for the samples. Default: None

        Returns
        -------
        np.array of shape `[n_samples, self.Y_dim]`
            samples

        """
        return NotImplemented

    @abstractmethod
    def get_hpd_interval(self):
        """Get the highest posterior density (HPD) interval

        """
        return NotImplemented

    def transform_back(self, tensor):
        """Transform back, i.e. unwhiten and unlog, the tensor

        Parameters
        ----------
        tensor : torch.Tensor of shape `[batch_size, Y_dim]`

        Returns
        -------
        torch.tensor of shape `[batch_size, Y_dim]`
            the original tensor

        """
        tensor = np.expand_dims(tensor, axis=1)
        tensor = self.unwhiten_back(tensor)
        tensor = self.exponentiate_back(tensor)
        return tensor.squeeze()

    def sample_low_rank(self, n_samples, mu, logvar, F):
        """Sample from a single Gaussian posterior with a full but low-rank plus diagonal covariance matrix

        Parameters
        ----------
        n_samples : int
            how many samples to obtain
        mu : torch.Tensor of shape `[self.batch_size, self.Y_dim]`
            network prediction of the mu (mean parameter) of the BNN posterior
        logvar : torch.Tensor of shape `[self.batch_size, self.Y_dim]`
            network prediction of the log of the diagonal elements of the covariance matrix
        F : torch.Tensor of shape `[self.batch_size, self.Y_dim, self.rank]`
            network prediction of the low rank portion of the covariance matrix

        Returns
        -------
        np.array of shape `[self.batch_size, n_samples, self.Y_dim]`
            samples

        """
        #F = torch.unsqueeze(F, dim=1).repeat(1, n_samples, 1, 1) # [self.batch_size, n_samples, self.Y_dim, self.rank]
        F = np.repeat(F, repeats=n_samples, axis=0) # [self.batch_size*n_samples, self.Y_dim, self.rank]
        mu = np.repeat(mu, repeats=n_samples, axis=0) # [self.batch_size*n_samples, self.Y_dim]
        logvar = np.repeat(logvar, repeats=n_samples, axis=0) # [self.batch_size*n_samples, self.Y_dim]
        eps_low_rank = np.random.randn(self.batch_size*n_samples, self.rank, 1)
        eps_diag = np.random.randn(self.batch_size*n_samples, self.Y_dim)
        half_var = np.exp(0.5*logvar) # [self.batch_size*n_samples, self.Y_dim]
        samples = np.matmul(F, eps_low_rank).squeeze() + mu + half_var*eps_diag # [self.batch_size*n_samples, self.Y_dim]
        samples = samples.reshape(self.batch_size, n_samples, self.Y_dim)
        #samples = samples.transpose((1, 0, 2)) # [self.batch_size, n_samples, self.Y_dim]
        print("before", samples[:, :, 2])
        if self.whitened_Y_cols_idx is not None:
            samples = self.unwhiten_back(samples)
        print("after unwhiten", samples[:, :, 2])
        if self.log_parameterized_Y_cols_idx is not None:
            samples = self.exponentiate_back(samples)
        print("after exp", samples[:, :, 2])
        return samples

    def unwhiten_back(self, sample):
        """Scale and shift back to the unwhitened state

        Parameters
        ----------
        pred : torch.Tensor
            network prediction of shape `[batch_size, n_samples, self.Y_dim]`

        Returns
        -------
        torch.Tensor
            the unwhitened pred
        
        """
        unwhitened_sub = sample[:, :, self.whitened_Y_cols_idx] # [batch_size, n_samples, n_cols_to_whiten]
        unwhitened_sub = unwhitened_sub*self.Y_std[:, np.newaxis, :] + self.Y_mean[:, np.newaxis, :]
        sample[:, :, self.whitened_Y_cols_idx] = unwhitened_sub
        return sample

    def exponentiate_back(self, sample):
        """Exponentiate back the log-parameterized Y values

        Parameters
        ----------
        pred : torch.Tensor
            network prediction of shape `[batch_size, n_samples, self.Y_dim]`

        Returns
        -------
        torch.Tensor
            the unwhitened pred
        
        """
        sample[:, :, self.log_parameterized_Y_cols_idx] = np.exp(sample[:, :, self.log_parameterized_Y_cols_idx])
        return sample

class DiagonalGaussianBNNPosterior(BaseGaussianBNNPosterior):
    """The negative log likelihood (NLL) for a single Gaussian with diagonal covariance matrix
        
    `BaseGaussianNLL.__init__` docstring for the parameter description.

    """
    def __init__(self, Y_dim, device, whitened_Y_cols_idx=None, Y_mean=None, Y_std=None, log_parameterized_Y_cols_idx=None):
        super(DiagonalGaussianBNNPosterior, self).__init__(Y_dim, device, whitened_Y_cols_idx, Y_mean, Y_std, log_parameterized_Y_cols_idx)
        self.out_dim = self.Y_dim*2

    def set_sliced_pred(self, pred):
        d = self.Y_dim # for readability
        pred = pred.cpu().numpy()
        self.batch_size = pred.shape[0]
        self.mu = pred[:, :d]
        self.logvar = pred[:, d:]
        #self.cov_diag = np.exp(self.logvar)
        #F_tran_F = np.matmul(normal.F, np.swapaxes(normal.F, 1, 2))
        #cov_mat = np.apply_along_axis(np.diag, -1, np.exp(normal.logvar)) + F_tran_F
        #cov_diag = np.exp(normal.logvar) + np.diagonal(F_tran_F, axis1=1, axis2=2)
        #assert np.array_equal(cov_mat.shape, [batch_size, self.Y_dim, self.Y_dim])
        #assert np.array_equal(cov_diag.shape, [batch_size, self.Y_dim])
        #np.apply_along_axis(np.diag, -1, np.exp(logvar)) # for diagonal

    def sample(self, n_samples, sample_seed):
        """Sample from a Gaussian posterior with diagonal covariance matrix

        Parameters
        ----------
        n_samples : int
            how many samples to obtain
        sample_seed : int
            seed for the samples. Default: None

        Returns
        -------
        np.array of shape `[n_samples, self.Y_dim]`
            samples

        """
        self.seed_samples(sample_seed)
        eps = np.random.randn(self.batch_size, n_samples, self.Y_dim)
        samples = eps*np.exp(0.5*np.expand_dims(self.logvar, axis=1)) + np.expand_dims(self.mu, 1)
        if self.whitened_Y_cols_idx is not None:
            samples = self.unwhiten_back(samples)
        if self.log_parameterized_Y_cols_idx is not None:
            samples = self.exponentiate_back(samples)
        return samples

    def get_hpd_interval(self):
        return NotImplementedError

class LowRankGaussianBNNPosterior(BaseGaussianBNNPosterior):
    """The negative log likelihood (NLL) for a single Gaussian with diagonal covariance matrix
        
    `BaseGaussianNLL.__init__` docstring for the parameter description.

    """
    def __init__(self, Y_dim, device, whitened_Y_cols_idx=None, Y_mean=None, Y_std=None, log_parameterized_Y_cols_idx=None):
        super(LowRankGaussianBNNPosterior, self).__init__(Y_dim, device, whitened_Y_cols_idx, Y_mean, Y_std, log_parameterized_Y_cols_idx)
        self.out_dim = self.Y_dim*4
        self.rank = 2 # FIXME: hardcoded

    def set_sliced_pred(self, pred):
        d = self.Y_dim # for readability
        pred = pred.cpu().numpy()
        self.batch_size = pred.shape[0]
        self.mu = pred[:, :d]
        self.logvar = pred[:, d:2*d]
        self.F = pred[:, 2*d:].reshape([self.batch_size, self.Y_dim, self.rank])
        #F_tran_F = np.matmul(self.F, np.swapaxes(self.F, 1, 2))
        #self.cov_diag = np.exp(self.logvar) + np.diagonal(F_tran_F, axis1=1, axis2=2)

    def sample(self, n_samples, sample_seed):
        self.seed_samples(sample_seed)
        return self.sample_low_rank(n_samples, self.mu, self.logvar, self.F)

    def get_hpd_interval(self):
        return NotImplementedError

class DoubleGaussianBNNPosterior(BaseGaussianBNNPosterior):
    """The negative log likelihood (NLL) for a single Gaussian with diagonal covariance matrix
        
    `BaseGaussianNLL.__init__` docstring for the parameter description.

    """
    def __init__(self, Y_dim, device, whitened_Y_cols_idx=None, Y_mean=None, Y_std=None, log_parameterized_Y_cols_idx=None):
        super(DoubleGaussianBNNPosterior, self).__init__(Y_dim, device, whitened_Y_cols_idx, Y_mean, Y_std, log_parameterized_Y_cols_idx)
        self.out_dim = self.Y_dim*8 + 1
        self.rank = 2 # FIXME: hardcoded

    def set_sliced_pred(self, pred):
        d = self.Y_dim # for readability
        self.w2 = 0.5*self.sigmoid(pred[:, -1].reshape(-1, 1)).cpu().numpy()
        pred = pred.cpu().numpy()
        self.batch_size = pred.shape[0]
        self.mu = pred[:, :d]
        self.logvar = pred[:, d:2*d]
        self.F = pred[:, 2*d:4*d].reshape([self.batch_size, self.Y_dim, self.rank])
        #F_tran_F = np.matmul(self.F, np.swapaxes(self.F, 1, 2))
        #self.cov_diag = np.exp(self.logvar) + np.diagonal(F_tran_F, axis1=1, axis2=2)
        self.mu2 = pred[:, 4*d:5*d]
        self.logvar2 = pred[:, 5*d:6*d]
        self.F2 = pred[:, 6*d:8*d].reshape([self.batch_size, self.Y_dim, self.rank])
        #F_tran_F2 = np.matmul(self.F2, np.swapaxes(self.F2, 1, 2))
        #self.cov_diag2 = np.exp(self.logvar2) + np.diagonal(F_tran_F2, axis1=1, axis2=2)
        
        
    def sample(self, n_samples, sample_seed):
        """Sample from a mixture of two Gaussians, each with a full but constrained as low-rank plus diagonal covariance

        Parameters
        ----------
        n_samples : int
            how many samples to obtain
        sample_seed : int
            seed for the samples. Default: None

        Returns
        -------
        np.array of shape `[self.batch_size, n_samples, self.Y_dim]`
            samples

        """
        self.seed_samples(sample_seed)
        samples = np.zeros([self.batch_size, n_samples, self.Y_dim])
        # Determine first vs. second Gaussian
        unif2 = np.random.rand(self.batch_size, n_samples)
        second_gaussian = (self.w2 > unif2)
        # Sample from second Gaussian
        samples2 = self.sample_low_rank(n_samples, self.mu2, self.logvar2, self.F2)
        samples[second_gaussian, :] = samples2[second_gaussian, :]
        # Sample from first Gaussian
        samples1 = self.sample_low_rank(n_samples, self.mu, self.logvar, self.F)
        samples[~second_gaussian, :] = samples1[~second_gaussian, :]
        #if self.whitened_Y_cols_idx is not None:
        #    samples = self.unwhiten_back(samples)
        #if self.log_parameterized_Y_cols_idx is not None:
        #    samples = self.exponentiate_back(samples)
        return samples

    def get_hpd_interval(self):
        return NotImplementedError




