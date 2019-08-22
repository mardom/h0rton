from types import SimpleNamespace as SNS
import torch

cfg = SNS()

# Global configs
cfg.DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
cfg.GLOBAL_SEED = 1113

# Data
cfg.DATA = SNS()
cfg.DATA.TRAIN = '/media/joshua/HDD_fun2/time_delay_challenge/train_seed123'
cfg.DATA.VAL = '/media/joshua/HDD_fun2/time_delay_challenge/val_seed111'
cfg.DATA.NORMALIZE = True
cfg.DATA.MEAN = [0.485, 0.456, 0.406]
cfg.DATA.STD = [0.229, 0.224, 0.225]
cfg.DATA.X_DIM = 224
cfg.DATA.Y_COLS = ['theta_E', 'gamma', 'center_x', 'center_y', 'e1', 'e2', 'gamma_ext', 'psi_ext', 'source_x', 'source_y', 'source_n_sersic', 'source_R_sersic', 'sersic_source_e1', 'sersic_source_e2', 'lens_light_e1', 'lens_light_e2', 'lens_light_n_sersic', 'lens_light_R_sersic']
cfg.DATA.Y_DIM = 18

# Model
cfg.MODEL = SNS()
cfg.MODEL.LOAD_PRETRAINED = True
if cfg.MODEL.LOAD_PRETRAINED:
    # Pretrained model expects exactly this normalization
    cfg.DATA.MEAN = [0.485, 0.456, 0.406]
    cfg.DATA.STD = [0.229, 0.224, 0.225]
cfg.MODEL.TYPE = 'mixture'
if cfg.MODEL.TYPE == 'diagonal': # a single Gaussian w. diagonal cov
    # y_dim for the means + y_dim for the cov
    cfg.MODEL.OUT_DIM = cfg.DATA.Y_DIM*2
elif cfg.MODEL.TYPE == 'low_rank': # a single Gaussian w. rank-2 cov
# y_dim for the means + 3*y_dim for the cov
    cfg.MODEL.OUT_DIM = cfg.DATA.Y_DIM*4
elif cfg.MODEL.TYPE == 'mixture': # two Gaussians each w. rank-2 cov
    # y_dim for the means + 2*y_dim for the cov for each Gaussian + 1 for amplitude weighting
    cfg.MODEL.OUT_DIM = cfg.DATA.Y_DIM*8 + 1
else:
    raise NotImplementedError

# Optimization
cfg.OPTIM = SNS()
cfg.OPTIM.N_EPOCHS = 10
cfg.OPTIM.BATCH_SIZE = 20
cfg.OPTIM.LEARNING_RATE = 1.e-4

# Logging
cfg.LOG = SNS()
cfg.LOG.CHECKPOINT_DIR = 'saved_models' # where to store saved models
cfg.LOG.CHECKPOINT_INTERVAL = 1 # in epochs
