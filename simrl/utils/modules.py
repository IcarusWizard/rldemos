import torch
from torch import nn
from torch.functional import F

from .dists import *
from simrl.utils.general import soft_clamp

ACTIVATION_CREATERS = {
    'relu' : lambda dim: nn.ReLU(inplace=True),
    'elu' : lambda dim: nn.ELU(),
    'leakyrelu' : lambda dim: nn.LeakyReLU(negative_slope=0.1, inplace=True),
    'tanh' : lambda dim: nn.Tanh(),
    'sigmoid' : lambda dim: nn.Sigmoid(),
    'identity' : lambda dim: nn.Identity(),
    'prelu' : lambda dim: nn.PReLU(dim),
}

class MLP(nn.Module):
    r"""
        Multi-layer Perceptron
        Inputs: 
        
            in_features : int, features numbers of the input
            out_features : int, features numbers of the output
            hidden_features : int, features numbers of the hidden layers
            hidden_layers : int, numbers of the hidden layers
            norm : str, normalization method between hidden layers, default : None
            hidden_activation : str, activation function used in hidden layers, default : 'leakyrelu'
            output_activation : str, activation function used in output layer, default : 'identity'
    """
    def __init__(self, in_features, out_features, hidden_features, hidden_layers, 
                 norm=None, hidden_activation='leakyrelu', output_activation='identity'):
        super(MLP, self).__init__()

        hidden_activation_creater = ACTIVATION_CREATERS[hidden_activation]
        output_activation_creater = ACTIVATION_CREATERS[output_activation]

        if hidden_layers == 0:
            self.net = nn.Sequential(
                nn.Linear(in_features, out_features),
                output_activation_creater(out_features)
            )
        else:
            net = []
            for i in range(hidden_layers):
                net.append(nn.Linear(in_features if i == 0 else hidden_features, hidden_features))
                if norm:
                    if norm == 'ln':
                        net.append(nn.LayerNorm(hidden_features))
                    elif norm == 'bn':
                        net.append(nn.BatchNorm1d(hidden_features))
                    else:
                        raise NotImplementedError(f'{norm} does not supported!')
                net.append(hidden_activation_creater(hidden_features))
            net.append(nn.Linear(hidden_features, out_features))
            net.append(output_activation_creater(out_features))
            self.net = nn.Sequential(*net)

    def forward(self, x):
        r"""forward method of MLP only assume the last dim of x matches `in_features`"""
        head_shape = x.shape[:-1]
        x = x.view(-1, x.shape[-1])
        out = self.net(x)
        out = out.view(*head_shape, out.shape[-1])
        return out

class DistributionWrapper(nn.Module):
    r"""wrap output of Module to distribution"""
    BASE_TYPES = ['normal', 'gmm', 'onehot']
    SUPPORTED_TYPES = BASE_TYPES + ['mix']

    def __init__(self, distribution_type='normal', **params):
        super().__init__()
        self.distribution_type = distribution_type
        self.params = params

        assert self.distribution_type in self.SUPPORTED_TYPES, f"{self.distribution_type} is not supported!"

        if self.distribution_type == 'normal':
            self.max_logstd = nn.Parameter(torch.ones(self.params['dim']) * 0, requires_grad=True)
            self.min_logstd = nn.Parameter(torch.ones(self.params['dim']) * -10, requires_grad=True)
            if not self.params.get('conditioned_std', True):
                self.logstd = nn.Parameter(torch.zeros(self.params['dim']), requires_grad=True)
        elif self.distribution_type == 'gmm':
            self.max_logstd = nn.Parameter(torch.ones(self.params['mixture'], self.params['dim']) * 0, requires_grad=True)
            self.min_logstd = nn.Parameter(torch.ones(self.params['mixture'], self.params['dim']) * -10, requires_grad=True)            
            if not self.params.get('conditioned_std', True):
                self.logstd = nn.Parameter(torch.zeros(self.params['mixture'], self.params['dim']), requires_grad=True)
        elif self.distribution_type == 'mix':
            assert 'dist_config' in self.params.keys(), "You need to provide `dist_config` for Mix distribution"

            self.dist_config = self.params['dist_config']
            self.wrapper_list = []
            self.input_sizes = []
            self.output_sizes = []
            for config in self.dist_config:
                dist_type = config['type']
                assert dist_type in self.SUPPORTED_TYPES, f"{dist_type} is not supported!"
                assert not dist_type == 'mix', "recursive MixDistribution is not supported!"

                self.wrapper_list.append(DistributionWrapper(dist_type, **config))

                self.input_sizes.append(config['dim'])
                self.output_sizes.append(config['output_dim'])
                
            self.wrapper_list = nn.ModuleList(self.wrapper_list)                                     

    def forward(self, x):
        if self.distribution_type == 'normal':
            if self.params.get('conditioned_std', True):
                mu, logstd = torch.chunk(x, 2, dim=-1)
            else:
                mu, logstd = x, self.logstd
            std = torch.exp(soft_clamp(logstd, self.min_logstd, self.max_logstd))
            return DiagnalNormal(mu, std)
        elif self.distribution_type == 'gmm':
            if self.params.get('conditioned_std', True):
                logits, mus, logstds = torch.split(x, [self.params['mixture'], 
                                                       self.params['mixture'] * self.params['dim'], 
                                                       self.params['mixture'] * self.params['dim']], dim=-1)
                mus = mus.view(*mus.shape[:-1], self.params['mixture'], self.params['dim'])      
                logstds = logstds.view(*logstds.shape[:-1], self.params['mixture'], self.params['dim'])
            else:
                logits, mus = torch.split(x, [self.params['mixture'], self.params['mixture'] * self.params['dim']], dim=-1)
                logstds = self.logstd
            stds = torch.exp(soft_clamp(logstds, self.min_logstd, self.max_logstd))
            return GaussianMixture(mus, stds, logits)
        elif self.distribution_type == 'onehot':
            return Onehot(10 * torch.tanh(x / 10)) # stabilize gradients
        elif self.distribution_type == 'mix':
            xs = torch.split(x, self.output_sizes, dim=-1)

            dists = [wrapper(x, _adapt_std, _payload) for x, _adapt_std, _payload, wrapper in zip(xs, self.wrapper_list)]
            
            return MixDistribution(dists)

    def extra_repr(self) -> str:
        return 'type={}, dim={}'.format(
            self.distribution_type, 
            self.params['dim'] if not self.distribution_type == 'mix' else len(self.wrapper_list)
        )

class ShareModule(torch.nn.Module):
    def get_weights(self):
        return {k : v.cpu() for k, v in self.state_dict().items()}

class OnehotActor(ShareModule):
    def __init__(self, state_dim, action_dim,
                 hidden_features=128,
                 hidden_layers=2,
                 norm=None,
                 hidden_activation='leakyrelu',
                 ):
        super().__init__()
        self.state_dim = state_dim
        self.action_dim = action_dim

        self.dist_net = torch.nn.Sequential(
            MLP(state_dim, action_dim, hidden_features, hidden_layers, norm, hidden_activation),
            DistributionWrapper(distribution_type='onehot', dim=action_dim)
        )

    def forward(self, state):
        return self.dist_net(state)

    @torch.no_grad()
    def act(self, state, sample_fn=lambda dist: dist.sample()):
        param = next(self.dist_net.parameters())
        device = param.device
        dtype = param.dtype
        state = torch.as_tensor(state, dtype=dtype, device=device).unsqueeze(0)
        action_dist = self.forward(state)
        action = sample_fn(action_dist)
        action = action.squeeze(0).numpy()
        return action

class ContinuousActor(ShareModule):
    def __init__(self, state_dim, action_dim,
                 hidden_features=128,
                 hidden_layers=2,
                 norm=None,
                 hidden_activation='leakyrelu',
                 ):
        super().__init__()
        self.state_dim = state_dim
        self.action_dim = action_dim

        self.dist_net = torch.nn.Sequential(
            MLP(state_dim, 2 * action_dim, hidden_features, hidden_layers, norm, hidden_activation),
            DistributionWrapper(distribution_type='gauss', dim=action_dim)
        )

    def forward(self, state):
        return self.dist_net(state)

    @torch.no_grad()
    def act(self, state, sample_fn=lambda dist: dist.sample()):
        param = next(self.dist_net.parameters())
        device = param.device
        dtype = param.dtype
        state = torch.as_tensor(state, dtype=dtype, device=device).unsqueeze(0)
        action_dist = self.forward(state)
        action = sample_fn(action_dist)
        action = action.squeeze(0).numpy()
        return action

class Critic(ShareModule):
    def __init__(self, state_dim, action_dim=None,
                 hidden_features=128,
                 hidden_layers=2,
                 norm=None,
                 hidden_activation='leakyrelu',
                 ):
        super().__init__()
        self.state_dim = state_dim
        self.action_dim = action_dim
        if self.action_dim: # Q funtion
            self.state_dim += self.action_dim

        self.value_net = MLP(self.state_dim, 1, hidden_features, hidden_layers, norm, hidden_activation)

    def forward(self, state, action=None):
        if self.action_dim:
            assert action is not None
            state = torch.cat([state, action], dim=-1)
        return self.value_net(state)