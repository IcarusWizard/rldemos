import argparse
import random
import os
import torch
import ray
import time
import copy
import numpy as np

from tqdm import tqdm
from tianshou.data import Batch

from utils import setup_seed, compute_gae
from utils.modules import OnehotActor, ContinuousActor, Critic
from utils.envs import make_env
from utils.data import Collector
from utils.logger import Logger

def get_ppo_config():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', type=str, default='CartPole-v0')
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--lambda', type=float, default=0.95)
    parser.add_argument('--epsilon', type=float, default=0.2)
    parser.add_argument('--grad_clip', type=float, default=0.5)
    parser.add_argument('--epoch', type=int, default=50)
    parser.add_argument('--step_per_epoch', type=int, default=2048)
    parser.add_argument('--batch_split', type=int, default=8)
    parser.add_argument('--ppo_run', type=int, default=8)
    parser.add_argument('--test-num', type=int, default=50)
    parser.add_argument('--log', type=str, default=None)
    parser.add_argument('--device', type=str, default='cuda' if torch.cuda.is_available() else 'cpu')

    parser.add_argument('-ahf', '--actor_hidden_features', type=int, default=128)
    parser.add_argument('-ahl', '--actor_hidden_layers', type=int, default=1)
    parser.add_argument('-aa', '--actor_activation', type=str, default='leakyrelu')
    parser.add_argument('-an', '--actor_norm', type=str, default=None)
    parser.add_argument('-chf', '--critic_hidden_features', type=int, default=128)
    parser.add_argument('-chl', '--critic_hidden_layers', type=int, default=1)
    parser.add_argument('-ca', '--critic_activation', type=str, default='leakyrelu')
    parser.add_argument('-cn', '--critic_norm', type=str, default=None)
    
    args = parser.parse_known_args()[0]
    
    return args.__dict__

class PPO:
    def __init__(self, config):
        self.config = config
        self.env = make_env(config)
        self.state_dim = self.env.observation_space.shape[0]
        self.action_dim = self.env.action_space.shape[0]
        self.device = torch.device(self.config['device'])

        if self.config['env_type'] == 'discrete':
            actor_class = OnehotActor
        elif self.config['env_type'] == 'continuous':
            actor_class = ContinuousActor
        else:
            raise ValueError('{} is not supported!'.format(self.config['env_type']))
        
        self.actor = actor_class(self.state_dim, self.action_dim,
                                hidden_features=self.config.get('actor_hidden_features', 128),
                                hidden_layers=self.config.get('actor_hidden_layers', 1),
                                hidden_activation=self.config.get('actor_activation', 'leakyrelu'),
                                norm=self.config.get('actor_norm', None)).to(self.device)

        self.critic = Critic(self.state_dim,
                             hidden_features=self.config.get('critic_hidden_features', 128),
                             hidden_layers=self.config.get('critic_hidden_layers', 1),
                             hidden_activation=self.config.get('critic_activation', 'leakyrelu'),
                             norm=self.config.get('critic_norm', None)).to(self.device)

        self.collector = Collector.remote(self.config, copy.deepcopy(self.actor))
        self.logger = Logger.remote(config, copy.deepcopy(self.actor))

        self.actor = self.actor.to(self.device)
        self.critic = self.critic.to(self.device)

        self.optimizor = torch.optim.Adam([*self.actor.parameters(), *self.critic.parameters()], lr=config['lr'])

    def run(self):
        for i in tqdm(range(self.config['epoch'])):
            batchs_id = self.collector.collect_steps.remote(self.config["step_per_epoch"], self.actor.state_dict())
            batchs = ray.get(batchs_id)

            batchs.to_torch(dtype=torch.float32, device=self.device)

            reward_std = batchs.reward.std()
            if not reward_std == 0:
                batchs.reward = (batchs.reward - batchs.reward.mean()) / reward_std

            action_dist = self.actor(batchs.obs)
            batchs.log_prob = action_dist.log_prob(batchs.action).detach()
            batchs.value = self.critic(batchs.obs).detach()
            batchs.adv, batchs.ret = compute_gae(batchs.reward, batchs.value, batchs.done, 
                                                 self.config['gamma'], self.config['lambda'])

            for _ in range(self.config['ppo_run']):
                for batch in batchs.split(self.config['batch_split']):
                    action_dist = self.actor(batch.obs)
                    log_prob = action_dist.log_prob(batch.action)
                    ratio = (log_prob - batch.log_prob).exp()

                    surr1 = ratio * batch.adv
                    surr2 = torch.clamp(ratio, 1 - self.config['epsilon'], 1 + self.config['epsilon']) * batch.adv
                    p_loss = - torch.min(surr1, surr2).mean()

                    value = self.critic(batch.obs)
                    v_loss = torch.mean((value - batch.ret) ** 2)

                    loss = p_loss + v_loss

                    self.optimizor.zero_grad()
                    loss.backward()
                    grad_norm = torch.nn.utils.clip_grad_norm_([*self.actor.parameters(), * self.critic.parameters()],
                                                            self.config['grad_clip'])       
                    self.optimizor.step()    

            self.logger.test_and_log.remote(self.actor.state_dict())

if __name__ == '__main__':
    ray.init()
    config = get_ppo_config()
    setup_seed(config['seed'] or random.randint(0, 1000000))
    experiment = PPO(config)
    experiment.run()
    ray.shutdown()