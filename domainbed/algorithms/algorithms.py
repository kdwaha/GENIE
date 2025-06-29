# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

import copy
from typing import List

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import numpy as np

from collections import OrderedDict

from domainbed import networks
from domainbed.lib.misc import random_pairs_of_minibatches
from domainbed.optimizers import get_optimizer

from domainbed.models.resnet_mixstyle import (
    resnet18_mixstyle_L234_p0d5_a0d1,
    resnet50_mixstyle_L234_p0d5_a0d1,
)
from domainbed.models.resnet_mixstyle2 import (
    resnet18_mixstyle2_L234_p0d5_a0d1,
    resnet50_mixstyle2_L234_p0d5_a0d1,
)

from domainbed.sagm import SAGM, LinearScheduler
#---------------------------------------------------------
from domainbed.gsam import GSAM, LinearScheduler
# from adabelief_pytorch import AdaBelief
import torch_optimizer as optim
from torch.distributions import Normal


def to_minibatch(x, y):
    minibatches = list(zip(x, y))
    return minibatches


class Algorithm(torch.nn.Module):
    """
    A subclass of Algorithm implements a domain generalization algorithm.
    Subclasses should implement the following:
    - update()
    - predict()
    """

    transforms = {}

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(Algorithm, self).__init__()
        self.input_shape = input_shape
        self.num_classes = num_classes
        self.num_domains = num_domains
        self.hparams = hparams
        self.best_val_acc = 0.0

    def update(self, x, y, **kwargs):
        """
        Perform one update step, given a list of (x, y) tuples for all
        environments.
        """
        raise NotImplementedError

    def predict(self, x):
        raise NotImplementedError

    def forward(self, x):
        return self.predict(x)

    def new_optimizer(self, parameters):
        optimizer = get_optimizer(
            self.hparams["optimizer"],
            parameters,
            lr=self.hparams["lr"],
            weight_decay=self.hparams["weight_decay"],
        )
        return optimizer

    def clone(self):
        clone = copy.deepcopy(self)
        clone.optimizer = self.new_optimizer(clone.network.parameters())
        clone.optimizer.load_state_dict(self.optimizer.state_dict())

        return clone


class ERM_ori(Algorithm):
    """
    Empirical Risk Minimization (ERM)
    """

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(ERM_ori, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)
        
        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(),
            lr=self.hparams["lr"],
            weight_decay=self.hparams["weight_decay"],
        )
        optimizer_type = type(self.optimizer).__name__
        print(f"Optimizer type: {optimizer_type}")

    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)

        self.optimizer.zero_grad()
        loss.backward()
        #grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])
        self.optimizer.step()

        #return {"loss": loss.item(), 'grads': grads}
        return {"loss": loss.item()}

    def predict(self, x):
        return self.network(x)
    
# #-----------------------------------------------------------
class ERM_new(Algorithm):
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(ERM_new, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)

        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(),
            lr=self.hparams["lr"],
            weight_decay=self.hparams["weight_decay"])

        self.pre_grads = None
        self.cumulative_g_change = None
        self.cumulative_weight_volume= None
        

    def compute_weight_volume_sampling(self, weight_matrix, num_samples=1000):
        """
        샘플링 기반 Weight Volume 계산 함수.

        Parameters:
            weight_matrix (torch.Tensor): 신경망 가중치 행렬
            num_samples (int): 샘플 개수 (default: 1000)

        Returns:
            float: Weight Volume 값
        """
        W = weight_matrix.detach().cpu().numpy()

        if len(W.shape) == 4:  # (out_channels, in_channels, kernel_height, kernel_width)
            W = W.reshape(W.shape[0], -1)  # (out_channels, flattened_kernel)


        # 가중치에서 일부만 샘플링
        num_features = W.shape[1] if len(W.shape) > 1 else W.shape[0]
        sampled_indices = np.random.choice(num_features, size=min(num_features, num_samples), replace=False)
        sampled_weights = W[:, sampled_indices] if len(W.shape) > 1 else W[sampled_indices]

        # 샘플링된 가중치에 대해 공분산 행렬 계산
        Sigma = np.cov(sampled_weights, rowvar=False)

        # 공분산 행렬이 특이행렬이 되는 경우를 방지하기 위해 작은 값 추가
        Sigma += np.eye(Sigma.shape[0]) * 1e-5  

        # 결정식(determinant) 계산
        det_Sigma = np.linalg.det(Sigma)

        # 공분산 행렬의 대각 원소 곱 계산
        diag_prod = np.prod(np.diag(Sigma))

        # Weight Volume 계산
        weight_volume = det_Sigma / diag_prod if diag_prod != 0 else 0

        return weight_volume

    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)
        self.optimizer.zero_grad()
        loss.backward()

        grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])

        grads_list = []
        for p in self.network.parameters():
            if p.grad is None:
                grads_list.append(torch.zeros_like(p).view(-1))
            else:
                grads_list.append(p.grad.view(-1))


        grads_2 = torch.cat(grads_list)
        if self.pre_grads is None:
            g_change = torch.zeros_like(grads_2)
        else:
            g_change = torch.abs(grads_2 - self.pre_grads)

        if self.cumulative_g_change is None:
            self.cumulative_g_change = g_change.clone()
        else:
            self.cumulative_g_change += g_change

        self.pre_grads = grads_2.clone()

        # weight_volumes = {}
        # for name, param in self.network.named_parameters():
        #     if "weight" in name and len(param.shape) > 1:  # Fully Connected / Conv 필터만 고려
        #         weight_volumes[name] = self.compute_weight_volume_sampling(param)
        #         print("weight_volumes[name]",name, weight_volumes[name])

        weight_volumes_list = []
        for name, param in self.network.named_parameters():
            if "weight" in name and len(param.shape) > 1:  # Fully Connected / Conv 필터만 고려
                wv = self.compute_weight_volume_sampling(param)
                weight_volumes_list.append(wv)

        # 한 스텝에서 하나의 Weight Volume 값 반환 (평균)
        if len(weight_volumes_list) > 0:
            weight_volume = sum(weight_volumes_list) / len(weight_volumes_list)
        else:
            weight_volume = 0

        # ✅ Weight Volume 누적 저장
        if self.cumulative_weight_volume is None:
            self.cumulative_weight_volume = torch.tensor(weight_volume)
        else:
            self.cumulative_weight_volume += torch.tensor(weight_volume)

        print("weight_volume: ", weight_volume)
        print(self.cumulative_weight_volume)

        self.optimizer.step()

        return {
            "loss": loss.item(),
            "grads": grads,
            "cumulative_g_change": self.cumulative_g_change
        }

    def predict(self, x):
        return self.network(x)
    

class ERM(Algorithm):
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(ERM, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)

        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(),
            lr=self.hparams["lr"],
            weight_decay=self.hparams["weight_decay"])

        self.pre_grads = None
        self.cumulative_g_change = None

    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)
        self.optimizer.zero_grad()
        loss.backward()

        grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])

        grads_list = []
        for p in self.network.parameters():
            if p.grad is None:
                grads_list.append(torch.zeros_like(p).view(-1))
            else:
                grads_list.append(p.grad.view(-1))
        grads_2 = torch.cat(grads_list)

        if self.pre_grads is None:
            g_change = torch.zeros_like(grads_2)
        else:
            g_change = torch.abs(grads_2 - self.pre_grads)

        if self.cumulative_g_change is None:
            self.cumulative_g_change = g_change.clone()
        else:
            self.cumulative_g_change += g_change

        self.pre_grads = grads_2.clone()

        self.optimizer.step()
        return {"loss": loss.item(), "grads": grads, "cumulative_g_change" : self.cumulative_g_change}

    def predict(self, x):
        return self.network(x)


class SGD(Algorithm):
    """
    Empirical Risk Minimization (ERM)
    """

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(SGD, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)
        self.optimizer = torch.optim.SGD( self.network.parameters(), lr=self.hparams["lr"], weight_decay=self.hparams["weight_decay"], momentum=0.9)
        optimizer_type = type(self.optimizer).__name__
        print(f"Optimizer type: {optimizer_type}")

    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)

        self.optimizer.zero_grad()
        loss.backward()
        #grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])
        self.optimizer.step()

        #return {"loss": loss.item(), 'grads': grads}
        return {"loss": loss.item()}

    def predict(self, x):
        return self.network(x)

#내 메소드----------------------------------------------------
class GENIE(Algorithm):
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(GENIE, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)

        # self.optimizer = get_optimizer(
        #     hparams["optimizer"],
        #     self.network.parameters(), 
        #     lr=self.hparams["lr"], 
        #     momentum=self.hparams["momentum"], 
        #     weight_decay=self.hparams['weight_decay'], 
        #     nesterov=True
        #     )
        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(), 
            lr=self.hparams["lr"],
            weight_decay=self.hparams['weight_decay']
            )
        
        self.prev_state = None
        self.gmean = None
        self.ge2 = None
        self.scale = None
        self.pre_grads = None
        self.cumulative_g_change = None
    
    def get_current_state(self):
        return {name: param.clone().detach() for name, param in self.network.named_parameters()}

    def set_state(self, new_state):
        for name, param in self.network.named_parameters():
            param.data.copy_(new_state[name])
            
    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)
        self.optimizer.zero_grad()
        loss.backward()

        grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])
        grads_list = []
        for p in self.network.parameters():
            if p.grad is None:
                grads_list.append(torch.zeros_like(p).view(-1))  # None인 경우 0으로 채움
            else:
                grads_list.append(p.grad.view(-1))
        grads_2 = torch.cat(grads_list)
        if self.pre_grads is None:
            g_change = torch.zeros_like(grads_2)  # 첫 번째 업데이트 시 변화량은 0
        else:
            g_change = torch.abs(grads_2 - self.pre_grads)
        if self.cumulative_g_change is None:
            self.cumulative_g_change = g_change.clone()
        else:
            self.cumulative_g_change += g_change  # 변화량 누적
        self.pre_grads = grads_2.clone()


        current_state = self.get_current_state()

        if self.prev_state is None:
            self._initialize_preconditioning(current_state)

        self._update_preconditioning(convergence_rate=self.hparams['convergence_rate'], moving_avg=self.hparams['moving_avg'] )
        

        self.set_state(self.prev_state)
        return {"loss": loss.item(), "grads": grads, "cumulative_g_change" : self.cumulative_g_change}

    def _initialize_preconditioning(self, current_state):
        self.prev_state = current_state
        self.gmean = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.ge2 = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.scale = 0.0

    def _update_preconditioning(self, convergence_rate, moving_avg):
        grad_sgd = {}
        pgrad = {}
        pGsnr = {}

        self.scale = (moving_avg * self.scale + 1.0)
        scale1 = (1 - moving_avg) * self.scale
        scale2 = 2.0 - scale1
        rho = (1.0 - moving_avg) * scale2 / (1.0 + moving_avg) / scale1

        with torch.no_grad():
            for k, param in self.network.named_parameters():
                delta = (param.grad.data).detach()
                self.gmean[k] = self.gmean[k] * moving_avg + delta * (1.0 - moving_avg)
                self.ge2[k] = self.ge2[k] * moving_avg + (delta ** 2) * (1.0 - moving_avg)
                gm = self.gmean[k] / scale1
                ge2 = self.ge2[k] / scale1
                var = ge2 - gm.square()
                var /= (1.0 - rho)
                var = torch.where(var > 0.0, var, (torch.zeros_like(var) + 1e-8))

                invvar = torch.clamp(1 / var, min=0.0, max=10.0)
                mvar = rho * var
                mvar = torch.where(mvar > 0.0, mvar, (torch.zeros_like(mvar) + 1e-8))
                
                tanh_invvar = torch.tanh(invvar)
                numerator = 1.0
                denominator = 1.0 + mvar / (gm.square() + 1e-8)
                pGsnr[k] = (numerator / denominator) * tanh_invvar

                noise_scale = torch.sum(tanh_invvar * torch.abs(gm) * (numerator / denominator)) / torch.sum(tanh_invvar)
                noise = torch.normal(torch.zeros_like(delta), torch.ones_like(delta))
                noise = noise * noise_scale
                grad_sgd[k] = (1 - tanh_invvar) * noise

            for k, param in self.network.named_parameters():
                pgrad[k] = self.gmean[k] / scale1 * pGsnr[k].view_as(param)

            for k, param in self.network.named_parameters():
                mask = (torch.rand_like(param) > self.hparams['p']).float() / (1-self.hparams['p'])
                self.prev_state[k] -= (pgrad[k] + grad_sgd[k]) * mask * convergence_rate

    def predict(self, x):
        return self.network(x)


class GENIE_Precondition(Algorithm):
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(GENIE_Precondition, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)

        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(), 
            lr=self.hparams["lr"], 
            momentum=self.hparams["momentum"], 
            weight_decay=self.hparams['weight_decay'], 
            nesterov=True
            )

        self.prev_state = None
        self.gmean = None
        self.ge2 = None
        self.scale = None
        self.pre_grads = None
        self.cumulative_g_change = None
    
    def get_current_state(self):
        return {name: param.clone().detach() for name, param in self.network.named_parameters()}

    def set_state(self, new_state):
        for name, param in self.network.named_parameters():
            param.data.copy_(new_state[name])
            
    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)
        self.optimizer.zero_grad()
        loss.backward()

        grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])
        grads_list = []
        for p in self.network.parameters():
            if p.grad is None:
                grads_list.append(torch.zeros_like(p).view(-1))  # None인 경우 0으로 채움
            else:
                grads_list.append(p.grad.view(-1))
        grads_2 = torch.cat(grads_list)
        if self.pre_grads is None:
            g_change = torch.zeros_like(grads_2)  # 첫 번째 업데이트 시 변화량은 0
        else:
            g_change = torch.abs(grads_2 - self.pre_grads)
        if self.cumulative_g_change is None:
            self.cumulative_g_change = g_change.clone()
        else:
            self.cumulative_g_change += g_change  # 변화량 누적
        self.pre_grads = grads_2.clone()


        current_state = self.get_current_state()

        if self.prev_state is None:
            self._initialize_preconditioning(current_state)

        self._update_preconditioning(convergence_rate=self.hparams['convergence_rate'], moving_avg=self.hparams['moving_avg'] )
        

        self.set_state(self.prev_state)
        return {"loss": loss.item(), "grads": grads, "cumulative_g_change" : self.cumulative_g_change}

    def _initialize_preconditioning(self, current_state):
        self.prev_state = current_state
        self.gmean = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.ge2 = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.scale = 0.0

    def _update_preconditioning(self, convergence_rate, moving_avg):
        grad_sgd = {}
        pgrad = {}
        pGsnr = {}

        self.scale = (moving_avg * self.scale + 1.0)
        scale1 = (1 - moving_avg) * self.scale
        scale2 = 2.0 - scale1
        rho = (1.0 - moving_avg) * scale2 / (1.0 + moving_avg) / scale1

        with torch.no_grad():
            for k, param in self.network.named_parameters():
                delta = (param.grad.data).detach()
                self.gmean[k] = self.gmean[k] * moving_avg + delta * (1.0 - moving_avg)
                self.ge2[k] = self.ge2[k] * moving_avg + (delta ** 2) * (1.0 - moving_avg)
                gm = self.gmean[k] / scale1
                ge2 = self.ge2[k] / scale1
                var = ge2 - gm.square()
                var /= (1.0 - rho)
                var = torch.where(var > 0.0, var, (torch.zeros_like(var) + 1e-8))

                invvar = torch.clamp(1 / var, min=0.0, max=10.0)
                mvar = rho * var
                mvar = torch.where(mvar > 0.0, mvar, (torch.zeros_like(mvar) + 1e-8))
                
                tanh_invvar = torch.tanh(invvar)
                numerator = 1.0
                denominator = 1.0 + mvar / (gm.square() + 1e-8)
                pGsnr[k] = (numerator / denominator) * tanh_invvar

                #noise_scale = torch.sum(tanh_invvar * torch.abs(gm) * (numerator / denominator)) / torch.sum(tanh_invvar)
                #noise = torch.normal(torch.zeros_like(delta), torch.ones_like(delta))
                #noise = noise * noise_scale
                #grad_sgd[k] = (1 - tanh_invvar) * noise

            for k, param in self.network.named_parameters():
                pgrad[k] = self.gmean[k] / scale1 * pGsnr[k].view_as(param)

            for k, param in self.network.named_parameters():
                #mask = (torch.rand_like(param) > self.hparams['p']).float() / (1-self.hparams['p'])
                #self.prev_state[k] -= (pgrad[k] + grad_sgd[k]) * mask * convergence_rate
                self.prev_state[k] -= (pgrad[k]) * convergence_rate

    def predict(self, x):
        return self.network(x)

class GENIE_Noise(Algorithm):
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(GENIE_Noise, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)

        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(), 
            lr=self.hparams["lr"], 
            momentum=0, 
            weight_decay=self.hparams['weight_decay'], 
            nesterov=False
            )

        self.prev_state = None
        self.gmean = None
        self.ge2 = None
        self.scale = None
        self.pre_grads = None
        self.cumulative_g_change = None
    
    def get_current_state(self):
        return {name: param.clone().detach() for name, param in self.network.named_parameters()}

    def set_state(self, new_state):
        for name, param in self.network.named_parameters():
            param.data.copy_(new_state[name])
            
    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)
        self.optimizer.zero_grad()
        loss.backward()

        grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])
        grads_list = []
        for p in self.network.parameters():
            if p.grad is None:
                grads_list.append(torch.zeros_like(p).view(-1))  # None인 경우 0으로 채움
            else:
                grads_list.append(p.grad.view(-1))
        grads_2 = torch.cat(grads_list)
        if self.pre_grads is None:
            g_change = torch.zeros_like(grads_2)  # 첫 번째 업데이트 시 변화량은 0
        else:
            g_change = torch.abs(grads_2 - self.pre_grads)
        if self.cumulative_g_change is None:
            self.cumulative_g_change = g_change.clone()
        else:
            self.cumulative_g_change += g_change  # 변화량 누적
        self.pre_grads = grads_2.clone()


        current_state = self.get_current_state()

        if self.prev_state is None:
            self._initialize_preconditioning(current_state)

        self._update_preconditioning(convergence_rate=self.hparams['convergence_rate'], moving_avg=self.hparams['moving_avg'] )
        

        self.set_state(self.prev_state)
        return {"loss": loss.item(), "grads": grads, "cumulative_g_change" : self.cumulative_g_change}

    def _initialize_preconditioning(self, current_state):
        self.prev_state = current_state
        self.gmean = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.ge2 = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.scale = 0.0

    def _update_preconditioning(self, convergence_rate, moving_avg):
        grad_sgd = {}
        pgrad = {}
        pGsnr = {}

        self.scale = (moving_avg * self.scale + 1.0)
        scale1 = (1 - moving_avg) * self.scale
        scale2 = 2.0 - scale1
        rho = (1.0 - moving_avg) * scale2 / (1.0 + moving_avg) / scale1

        with torch.no_grad():
            for k, param in self.network.named_parameters():
                delta = (param.grad.data).detach()
                self.gmean[k] = self.gmean[k] * moving_avg + delta * (1.0 - moving_avg)
                self.ge2[k] = self.ge2[k] * moving_avg + (delta ** 2) * (1.0 - moving_avg)
                gm = self.gmean[k] / scale1
                ge2 = self.ge2[k] / scale1
                var = ge2 - gm.square()
                var /= (1.0 - rho)
                var = torch.where(var > 0.0, var, (torch.zeros_like(var) + 1e-8))

                invvar = torch.clamp(1 / var, min=0.0, max=10.0)
                mvar = rho * var
                mvar = torch.where(mvar > 0.0, mvar, (torch.zeros_like(mvar) + 1e-8))
                
                tanh_invvar = torch.tanh(invvar)
                numerator = 1.0
                denominator = 1.0 + mvar / (gm.square() + 1e-8)
                pGsnr[k] = (numerator / denominator) * tanh_invvar

                noise_scale = torch.sum(tanh_invvar * torch.abs(gm) * (numerator / denominator)) / torch.sum(tanh_invvar)
                noise = torch.normal(torch.zeros_like(delta), torch.ones_like(delta))
                noise = noise * noise_scale
                grad_sgd[k] = (1 - tanh_invvar) * noise

            for k, param in self.network.named_parameters():
                pgrad[k] = self.gmean[k] / scale1 * pGsnr[k].view_as(param)

            for k, param in self.network.named_parameters():
                p = 0.0
                mask = (torch.rand_like(param) >= p).float() / (1-p)
                # mask = (torch.rand_like(param) > self.hparams['p']).float() / (1-self.hparams['p'])
                #self.prev_state[k] -= (pgrad[k] + grad_sgd[k]) * convergence_rate
                self.prev_state[k] -= (pgrad[k] + grad_sgd[k]) * mask * convergence_rate

    def predict(self, x):
        return self.network(x)
    
class GENIE_Mask(Algorithm):
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(GENIE_Mask, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)

        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(), 
            lr=self.hparams["lr"], 
            momentum=self.hparams["momentum"], 
            weight_decay=self.hparams['weight_decay'], 
            nesterov=True
            )

        self.prev_state = None
        self.gmean = None
        self.ge2 = None
        self.scale = None
        self.pre_grads = None
        self.cumulative_g_change = None
    
    def get_current_state(self):
        return {name: param.clone().detach() for name, param in self.network.named_parameters()}

    def set_state(self, new_state):
        for name, param in self.network.named_parameters():
            param.data.copy_(new_state[name])
            
    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)
        self.optimizer.zero_grad()
        loss.backward()

        grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])
        grads_list = []
        for p in self.network.parameters():
            if p.grad is None:
                grads_list.append(torch.zeros_like(p).view(-1))  # None인 경우 0으로 채움
            else:
                grads_list.append(p.grad.view(-1))
        grads_2 = torch.cat(grads_list)
        if self.pre_grads is None:
            g_change = torch.zeros_like(grads_2)  # 첫 번째 업데이트 시 변화량은 0
        else:
            g_change = torch.abs(grads_2 - self.pre_grads)
        if self.cumulative_g_change is None:
            self.cumulative_g_change = g_change.clone()
        else:
            self.cumulative_g_change += g_change  # 변화량 누적
        self.pre_grads = grads_2.clone()


        current_state = self.get_current_state()

        if self.prev_state is None:
            self._initialize_preconditioning(current_state)

        self._update_preconditioning(convergence_rate=self.hparams['convergence_rate'], moving_avg=self.hparams['moving_avg'] )
        

        self.set_state(self.prev_state)
        return {"loss": loss.item(), "grads": grads, "cumulative_g_change" : self.cumulative_g_change}

    def _initialize_preconditioning(self, current_state):
        self.prev_state = current_state
        self.gmean = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.ge2 = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.scale = 0.0

    def _update_preconditioning(self, convergence_rate, moving_avg):
        grad_sgd = {}
        pgrad = {}
        pGsnr = {}

        self.scale = (moving_avg * self.scale + 1.0)
        scale1 = (1 - moving_avg) * self.scale
        scale2 = 2.0 - scale1
        rho = (1.0 - moving_avg) * scale2 / (1.0 + moving_avg) / scale1

        with torch.no_grad():
            for k, param in self.network.named_parameters():
                delta = (param.grad.data).detach()
                self.gmean[k] = self.gmean[k] * moving_avg + delta * (1.0 - moving_avg)
                self.ge2[k] = self.ge2[k] * moving_avg + (delta ** 2) * (1.0 - moving_avg)
                gm = self.gmean[k] / scale1
                ge2 = self.ge2[k] / scale1
                var = ge2 - gm.square()
                var /= (1.0 - rho)
                var = torch.where(var > 0.0, var, (torch.zeros_like(var) + 1e-8))

                invvar = torch.clamp(1 / var, min=0.0, max=10.0)
                mvar = rho * var
                mvar = torch.where(mvar > 0.0, mvar, (torch.zeros_like(mvar) + 1e-8))
                
                tanh_invvar = torch.tanh(invvar)
                numerator = 1.0
                denominator = 1.0 + mvar / (gm.square() + 1e-8)
                pGsnr[k] = (numerator / denominator) * tanh_invvar

                # noise_scale = torch.sum(tanh_invvar * torch.abs(gm) * (numerator / denominator)) / torch.sum(tanh_invvar)
                # noise = torch.normal(torch.zeros_like(delta), torch.ones_like(delta))
                # noise = noise * noise_scale
                #grad_sgd[k] = (1 - tanh_invvar) * noise

            for k, param in self.network.named_parameters():
                pgrad[k] = self.gmean[k] / scale1 * pGsnr[k].view_as(param)

            for k, param in self.network.named_parameters():
                mask = (torch.rand_like(param) > self.hparams['p']).float() / (1-self.hparams['p'])
                #self.prev_state[k] -= (pgrad[k] + grad_sgd[k]) * mask * convergence_rate
                self.prev_state[k] -= (pgrad[k]) * mask * convergence_rate

    def predict(self, x):
        return self.network(x)
    

#optimizer --------------------------------------------------
# class MSVAG(Algorithm):

#     def __init__(self, input_shape, num_classes, num_domains, hparams):
#         super(MSVAG, self).__init__(input_shape, num_classes, num_domains, hparams)
#         self.featurizer = networks.Featurizer(input_shape, self.hparams)
#         self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
#         self.network = nn.Sequential(self.featurizer, self.classifier)
#         self.optimizer = optim.MSVAG(self.network.parameters(), lr= self.hparams["lr"], 
#                                           weight_decay=self.hparams["weight_decay"],
#                                           hessian_power=1.0)

#     def update(self, x, y, **kwargs):
#         all_x = torch.cat(x)
#         all_y = torch.cat(y)
#         loss = F.cross_entropy(self.predict(all_x), all_y)

#         self.optimizer.zero_grad()
#         loss.backward(create_graph = True)
#         self.optimizer.step()

#         return {"loss": loss.item()}

#     def predict(self, x):
#         return self.network(x)

class Adahessian(Algorithm):

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(Adahessian, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)

        self.optimizer = optim.Adahessian(self.network.parameters(), 
                                          lr= self.hparams["lr"], 
                                          eps=1e-8,
                                          weight_decay=self.hparams["weight_decay"],
                                          hessian_power=1.0)

    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)

        self.optimizer.zero_grad()
        loss.backward(create_graph = True)
        self.optimizer.step()

        return {"loss": loss.item()}

    def predict(self, x):
        return self.network(x)

class GSAM_DG(Algorithm):
    """
    Gradient Sharpness-Aware Minimization (GSAM) in implicit step style.
    """

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super().__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)
        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(),
            lr=self.hparams["lr"],
            weight_decay=self.hparams["weight_decay"],
            momentum=0.9
        )

        self.lr_scheduler = LinearScheduler(
            T_max=5000, max_value=self.hparams["lr"], min_value=self.hparams["lr"], optimizer=self.optimizer
        )

        self.rho_scheduler = LinearScheduler(T_max=5000, max_value=0.05, min_value=0.05)

        self.GSAM_optimizer = GSAM(
            params=self.network.parameters(),
            base_optimizer=self.optimizer,
            model=self.network,
            gsam_alpha=self.hparams["gsam_alpha"],
            rho_scheduler=self.rho_scheduler,
            adaptive=False,
        )

    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)

        def loss_fn(predictions, targets):
            return F.cross_entropy(predictions, targets)

        # Set closure for GSAM optimizer
        self.GSAM_optimizer.set_closure(loss_fn, all_x, all_y)

        # Perform the entire GSAM step implicitly
        predictions, loss = self.GSAM_optimizer.step()

        # Update learning rate and rho
        self.lr_scheduler.step()
        self.GSAM_optimizer.update_rho_t()

        return {"loss": loss.item()}

    def predict(self, x):
        return self.network(x)

#---------------------------------------------------
class SAGM_DG(Algorithm):
    """
    Empirical Risk Minimization (ERM)
    """

    # def __init__(self, input_shape, num_classes, num_domains, hparams):
    #     assert input_shape[1:3] == (224, 224), "Mixstyle support R18 and R50 only"
    #     super().__init__(input_shape, num_classes, num_domains, hparams)
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super().__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)
        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(),
            lr=self.hparams["lr"],
            weight_decay=self.hparams["weight_decay"],
            momentum=0.9
        )

        self.lr_scheduler = LinearScheduler(T_max=5000, max_value=self.hparams["lr"],
                                    min_value=self.hparams["lr"], optimizer=self.optimizer)

        self.rho_scheduler = LinearScheduler(T_max=5000, max_value=0.05,
                                         min_value=0.05)

        self.SAGM_optimizer = SAGM(params=self.network.parameters(), base_optimizer=self.optimizer, model=self.network,
                               alpha=self.hparams["sagm_alpha"], rho_scheduler=self.rho_scheduler, adaptive=False)

    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)

        def loss_fn(predictions, targets):
            return F.cross_entropy(predictions, targets)

        self.SAGM_optimizer.set_closure(loss_fn, all_x, all_y)
        predictions, loss = self.SAGM_optimizer.step()
        self.lr_scheduler.step()
        self.SAGM_optimizer.update_rho_t()


        return {"loss": loss.item()}

    def predict(self, x):
        return self.network(x)

class Mixstyle(Algorithm):
    """MixStyle w/o domain label (random shuffle)"""

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        assert input_shape[1:3] == (224, 224), "Mixstyle support R18 and R50 only"
        super().__init__(input_shape, num_classes, num_domains, hparams)
        if hparams["resnet18"]:
            network = resnet18_mixstyle_L234_p0d5_a0d1()
        else:
            network = resnet50_mixstyle_L234_p0d5_a0d1()
        self.featurizer = networks.ResNet(input_shape, self.hparams, network)

        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)
        self.optimizer = self.new_optimizer(self.network.parameters())

    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        return {"loss": loss.item()}

    def predict(self, x):
        return self.network(x)

class Mixstyle2(Algorithm):
    """MixStyle w/ domain label"""

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        assert input_shape[1:3] == (224, 224), "Mixstyle support R18 and R50 only"
        super().__init__(input_shape, num_classes, num_domains, hparams)
        if hparams["resnet18"]:
            network = resnet18_mixstyle2_L234_p0d5_a0d1()
        else:
            network = resnet50_mixstyle2_L234_p0d5_a0d1()
        self.featurizer = networks.ResNet(input_shape, self.hparams, network)

        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)
        self.optimizer = self.new_optimizer(self.network.parameters())

    def pair_batches(self, xs, ys):
        xs = [x.chunk(2) for x in xs]
        ys = [y.chunk(2) for y in ys]
        N = len(xs)
        pairs = []
        for i in range(N):
            j = i + 1 if i < (N - 1) else 0
            xi, yi = xs[i][0], ys[i][0]
            xj, yj = xs[j][1], ys[j][1]

            pairs.append(((xi, yi), (xj, yj)))

        return pairs

    def update(self, x, y, **kwargs):
        pairs = self.pair_batches(x, y)
        loss = 0.0

        for (xi, yi), (xj, yj) in pairs:
            #  Mixstyle2:
            #  For the input x, the first half comes from one domain,
            #  while the second half comes from the other domain.
            x2 = torch.cat([xi, xj])
            y2 = torch.cat([yi, yj])
            loss += F.cross_entropy(self.predict(x2), y2)

        loss /= len(pairs)

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        return {"loss": loss.item()}

    def predict(self, x):
        return self.network(x)

class ARM(ERM):
    """Adaptive Risk Minimization (ARM)"""

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        original_input_shape = input_shape
        input_shape = (1 + original_input_shape[0],) + original_input_shape[1:]
        super(ARM, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.context_net = networks.ContextNet(original_input_shape)
        self.support_size = hparams["batch_size"]

    def predict(self, x):
        batch_size, c, h, w = x.shape
        if batch_size % self.support_size == 0:
            meta_batch_size = batch_size // self.support_size
            support_size = self.support_size
        else:
            meta_batch_size, support_size = 1, batch_size
        context = self.context_net(x)
        context = context.reshape((meta_batch_size, support_size, 1, h, w))
        context = context.mean(dim=1)
        context = torch.repeat_interleave(context, repeats=support_size, dim=0)
        x = torch.cat([x, context], dim=1)
        return self.network(x)

class SAM(Algorithm):
    """Sharpness-Aware Minimization
    """
    @staticmethod
    def norm(tensor_list: List[torch.tensor], p=2):
        """Compute p-norm for tensor list"""
        return torch.cat([x.flatten() for x in tensor_list]).norm(p)
    
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(SAM, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)
        
        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(),
            lr=self.hparams["lr"],
            weight_decay=self.hparams["weight_decay"],
            momentum=0.9
        )
        self.pre_grads = None
        self.cumulative_g_change = None
        
    def update(self, x, y, **kwargs):
        all_x = torch.cat([xi for xi in x])
        all_y = torch.cat([yi for yi in y])
        loss = F.cross_entropy(self.predict(all_x), all_y)

        # 1. eps(w) = rho * g(w) / g(w).norm(2)
        #           = (rho / g(w).norm(2)) * g(w)
        grad_w = autograd.grad(loss, self.network.parameters())
        scale = self.hparams["rho"] / self.norm(grad_w)
        eps = [g * scale for g in grad_w]

        # 2. w' = w + eps(w)
        with torch.no_grad():
            for p, v in zip(self.network.parameters(), eps):
                p.add_(v)

        # 3. w = w - lr * g(w')
        loss = F.cross_entropy(self.predict(all_x), all_y)

        self.optimizer.zero_grad()
        loss.backward()
        # restore original network params
        with torch.no_grad():
            for p, v in zip(self.network.parameters(), eps):
                p.sub_(v)

        grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])

        grads_list = []
        for p in self.network.parameters():
            if p.grad is None:
                grads_list.append(torch.zeros_like(p).view(-1))
            else:
                grads_list.append(p.grad.view(-1))
        grads_2 = torch.cat(grads_list)

        if self.pre_grads is None:
            g_change = torch.zeros_like(grads_2)
        else:
            g_change = torch.abs(grads_2 - self.pre_grads)

        if self.cumulative_g_change is None:
            self.cumulative_g_change = g_change.clone()
        else:
            self.cumulative_g_change += g_change

        self.pre_grads = grads_2.clone()


        self.optimizer.step()
        return {"loss": loss.item(), "grads": grads, "cumulative_g_change" : self.cumulative_g_change}

    
    def predict(self, x):
        return self.network(x)

class AbstractDANN(Algorithm):
    """Domain-Adversarial Neural Networks (abstract class)"""

    def __init__(self, input_shape, num_classes, num_domains, hparams, conditional, class_balance):

        super(AbstractDANN, self).__init__(input_shape, num_classes, num_domains, hparams)

        self.register_buffer("update_count", torch.tensor([0]))
        self.conditional = conditional
        self.class_balance = class_balance

        # Algorithms
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.discriminator = networks.MLP(self.featurizer.n_outputs, num_domains, self.hparams)
        self.class_embeddings = nn.Embedding(num_classes, self.featurizer.n_outputs)

        # Optimizers
        self.disc_opt = get_optimizer(
            hparams["optimizer"],
            (list(self.discriminator.parameters()) + list(self.class_embeddings.parameters())),
            lr=self.hparams["lr_d"],
            weight_decay=self.hparams["weight_decay_d"],
            betas=(self.hparams["beta1"], 0.9),
        )

        self.gen_opt = get_optimizer(
            hparams["optimizer"],
            (list(self.featurizer.parameters()) + list(self.classifier.parameters())),
            lr=self.hparams["lr_g"],
            weight_decay=self.hparams["weight_decay_g"],
            betas=(self.hparams["beta1"], 0.9),
        )

    def update(self, x, y, **kwargs):
        self.update_count += 1
        all_x = torch.cat([xi for xi in x])
        all_y = torch.cat([yi for yi in y])
        minibatches = to_minibatch(x, y)
        all_z = self.featurizer(all_x)
        if self.conditional:
            disc_input = all_z + self.class_embeddings(all_y)
        else:
            disc_input = all_z
        disc_out = self.discriminator(disc_input)
        disc_labels = torch.cat(
            [
                torch.full((x.shape[0],), i, dtype=torch.int64, device="cuda")
                for i, (x, y) in enumerate(minibatches)
            ]
        )

        if self.class_balance:
            y_counts = F.one_hot(all_y).sum(dim=0)
            weights = 1.0 / (y_counts[all_y] * y_counts.shape[0]).float()
            disc_loss = F.cross_entropy(disc_out, disc_labels, reduction="none")
            disc_loss = (weights * disc_loss).sum()
        else:
            disc_loss = F.cross_entropy(disc_out, disc_labels)

        disc_softmax = F.softmax(disc_out, dim=1)
        input_grad = autograd.grad(
            disc_softmax[:, disc_labels].sum(), [disc_input], create_graph=True
        )[0]
        grad_penalty = (input_grad ** 2).sum(dim=1).mean(dim=0)
        disc_loss += self.hparams["grad_penalty"] * grad_penalty

        d_steps_per_g = self.hparams["d_steps_per_g_step"]
        if self.update_count.item() % (1 + d_steps_per_g) < d_steps_per_g:

            self.disc_opt.zero_grad()
            disc_loss.backward()
            self.disc_opt.step()
            return {"disc_loss": disc_loss.item()}
        else:
            all_preds = self.classifier(all_z)
            classifier_loss = F.cross_entropy(all_preds, all_y)
            gen_loss = classifier_loss + (self.hparams["lambda"] * -disc_loss)
            self.disc_opt.zero_grad()
            self.gen_opt.zero_grad()
            gen_loss.backward()
            self.gen_opt.step()
            return {"gen_loss": gen_loss.item()}

    def predict(self, x):
        return self.classifier(self.featurizer(x))

class DANN(AbstractDANN):
    """Unconditional DANN"""

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(DANN, self).__init__(
            input_shape,
            num_classes,
            num_domains,
            hparams,
            conditional=False,
            class_balance=False,
        )

class CDANN(AbstractDANN):
    """Conditional DANN"""

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(CDANN, self).__init__(
            input_shape,
            num_classes,
            num_domains,
            hparams,
            conditional=True,
            class_balance=True,
        )

class IRM(ERM):
    """Invariant Risk Minimization"""

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(IRM, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.register_buffer("update_count", torch.tensor([0]))

    @staticmethod
    def _irm_penalty(logits, y):
        scale = torch.tensor(1.0).cuda().requires_grad_()
        loss_1 = F.cross_entropy(logits[::2] * scale, y[::2])
        loss_2 = F.cross_entropy(logits[1::2] * scale, y[1::2])
        grad_1 = autograd.grad(loss_1, [scale], create_graph=True)[0]
        grad_2 = autograd.grad(loss_2, [scale], create_graph=True)[0]
        result = torch.sum(grad_1 * grad_2)
        return result

    def update(self, x, y, **kwargs):
        minibatches = to_minibatch(x, y)
        penalty_weight = (
            self.hparams["irm_lambda"]
            if self.update_count >= self.hparams["irm_penalty_anneal_iters"]
            else 1.0
        )
        nll = 0.0
        penalty = 0.0

        all_x = torch.cat([x for x, y in minibatches])
        all_logits = self.network(all_x)
        all_logits_idx = 0
        for i, (x, y) in enumerate(minibatches):
            logits = all_logits[all_logits_idx : all_logits_idx + x.shape[0]]
            all_logits_idx += x.shape[0]
            nll += F.cross_entropy(logits, y)
            penalty += self._irm_penalty(logits, y)
        nll /= len(minibatches)
        penalty /= len(minibatches)
        loss = nll + (penalty_weight * penalty)

        if self.update_count == self.hparams["irm_penalty_anneal_iters"]:
            # Reset Adam, because it doesn't like the sharp jump in gradient
            # magnitudes that happens at this step.
            self.optimizer = get_optimizer(
                self.hparams["optimizer"],
                self.network.parameters(),
                lr=self.hparams["lr"],
                weight_decay=self.hparams["weight_decay"],
            )

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        self.update_count += 1
        return {"loss": loss.item(), "nll": nll.item(), "penalty": penalty.item()}

class VREx(ERM):
    """V-REx algorithm from http://arxiv.org/abs/2003.00688"""

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(VREx, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.register_buffer("update_count", torch.tensor([0]))

    def update(self, x, y, **kwargs):
        minibatches = to_minibatch(x, y)
        if self.update_count >= self.hparams["vrex_penalty_anneal_iters"]:
            penalty_weight = self.hparams["vrex_lambda"]
        else:
            penalty_weight = 1.0

        nll = 0.0

        all_x = torch.cat([x for x, y in minibatches])
        all_logits = self.network(all_x)
        all_logits_idx = 0
        losses = torch.zeros(len(minibatches))
        for i, (x, y) in enumerate(minibatches):
            logits = all_logits[all_logits_idx : all_logits_idx + x.shape[0]]
            all_logits_idx += x.shape[0]
            nll = F.cross_entropy(logits, y)
            losses[i] = nll

        mean = losses.mean()
        penalty = ((losses - mean) ** 2).mean()
        loss = mean + penalty_weight * penalty

        if self.update_count == self.hparams["vrex_penalty_anneal_iters"]:
            # Reset Adam (like IRM), because it doesn't like the sharp jump in
            # gradient magnitudes that happens at this step.
            self.optimizer = get_optimizer(
                self.hparams["optimizer"],
                self.network.parameters(),
                lr=self.hparams["lr"],
                weight_decay=self.hparams["weight_decay"],
            )

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        self.update_count += 1
        return {"loss": loss.item(), "nll": nll.item(), "penalty": penalty.item()}

class Mixup(ERM):
    """
    Mixup of minibatches from different domains
    https://arxiv.org/pdf/2001.00677.pdf
    https://arxiv.org/pdf/1912.01805.pdf
    """

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(Mixup, self).__init__(input_shape, num_classes, num_domains, hparams)

    def update(self, x, y, **kwargs):
        minibatches = to_minibatch(x, y)
        objective = 0

        for (xi, yi), (xj, yj) in random_pairs_of_minibatches(minibatches):
            lam = np.random.beta(self.hparams["mixup_alpha"], self.hparams["mixup_alpha"])

            x = lam * xi + (1 - lam) * xj
            predictions = self.predict(x)

            objective += lam * F.cross_entropy(predictions, yi)
            objective += (1 - lam) * F.cross_entropy(predictions, yj)

        objective /= len(minibatches)

        self.optimizer.zero_grad()
        objective.backward()
        self.optimizer.step()

        return {"loss": objective.item()}

class OrgMixup(ERM):
    """
    Original Mixup independent with domains
    """

    def update(self, x, y, **kwargs):
        x = torch.cat(x)
        y = torch.cat(y)

        indices = torch.randperm(x.size(0))
        x2 = x[indices]
        y2 = y[indices]

        lam = np.random.beta(self.hparams["mixup_alpha"], self.hparams["mixup_alpha"])

        x = lam * x + (1 - lam) * x2
        predictions = self.predict(x)

        objective = lam * F.cross_entropy(predictions, y)
        objective += (1 - lam) * F.cross_entropy(predictions, y2)

        self.optimizer.zero_grad()
        objective.backward()
        self.optimizer.step()

        return {"loss": objective.item()}

class CutMix(ERM):
    @staticmethod
    def rand_bbox(size, lam):
        W = size[2]
        H = size[3]
        cut_rat = np.sqrt(1.0 - lam)
        cut_w = np.int(W * cut_rat)
        cut_h = np.int(H * cut_rat)

        # uniform
        cx = np.random.randint(W)
        cy = np.random.randint(H)

        bbx1 = np.clip(cx - cut_w // 2, 0, W)
        bby1 = np.clip(cy - cut_h // 2, 0, H)
        bbx2 = np.clip(cx + cut_w // 2, 0, W)
        bby2 = np.clip(cy + cut_h // 2, 0, H)

        return bbx1, bby1, bbx2, bby2

    def update(self, x, y, **kwargs):
        # cutmix_prob is set to 1.0 for ImageNet and 0.5 for CIFAR100 in the original paper.
        x = torch.cat(x)
        y = torch.cat(y)

        r = np.random.rand(1)
        if self.hparams["beta"] > 0 and r < self.hparams["cutmix_prob"]:
            # generate mixed sample
            beta = self.hparams["beta"]
            lam = np.random.beta(beta, beta)
            rand_index = torch.randperm(x.size()[0]).cuda()
            target_a = y
            target_b = y[rand_index]
            bbx1, bby1, bbx2, bby2 = self.rand_bbox(x.size(), lam)
            x[:, :, bbx1:bbx2, bby1:bby2] = x[rand_index, :, bbx1:bbx2, bby1:bby2]
            # adjust lambda to exactly match pixel ratio
            lam = 1 - ((bbx2 - bbx1) * (bby2 - bby1) / (x.size()[-1] * x.size()[-2]))
            # compute output
            output = self.predict(x)
            objective = F.cross_entropy(output, target_a) * lam + F.cross_entropy(
                output, target_b
            ) * (1.0 - lam)
        else:
            output = self.predict(x)
            objective = F.cross_entropy(output, y)

        self.optimizer.zero_grad()
        objective.backward()
        self.optimizer.step()

        return {"loss": objective.item()}

class GroupDRO(ERM):
    """
    Robust ERM minimizes the error at the worst minibatch
    Algorithm 1 from [https://arxiv.org/pdf/1911.08731.pdf]
    """

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(GroupDRO, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.register_buffer("q", torch.Tensor())

    def update(self, x, y, **kwargs):
        minibatches = to_minibatch(x, y)
        device = "cuda" if minibatches[0][0].is_cuda else "cpu"

        if not len(self.q):
            self.q = torch.ones(len(minibatches)).to(device)

        losses = torch.zeros(len(minibatches)).to(device)

        for m in range(len(minibatches)):
            x, y = minibatches[m]
            losses[m] = F.cross_entropy(self.predict(x), y)
            self.q[m] *= (self.hparams["groupdro_eta"] * losses[m].data).exp()

        self.q /= self.q.sum()

        loss = torch.dot(losses, self.q) / len(minibatches)

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        return {"loss": loss.item()}

class MLDG(ERM):
    """
    Model-Agnostic Meta-Learning
    Algorithm 1 / Equation (3) from: https://arxiv.org/pdf/1710.03463.pdf
    Related: https://arxiv.org/pdf/1703.03400.pdf
    Related: https://arxiv.org/pdf/1910.13580.pdf
    """

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(MLDG, self).__init__(input_shape, num_classes, num_domains, hparams)

    def update(self, x, y, **kwargs):
        """
        Terms being computed:
            * Li = Loss(xi, yi, params)
            * Gi = Grad(Li, params)

            * Lj = Loss(xj, yj, Optimizer(params, grad(Li, params)))
            * Gj = Grad(Lj, params)

            * params = Optimizer(params, Grad(Li + beta * Lj, params))
            *        = Optimizer(params, Gi + beta * Gj)

        That is, when calling .step(), we want grads to be Gi + beta * Gj

        For computational efficiency, we do not compute second derivatives.
        """
        minibatches = to_minibatch(x, y)
        num_mb = len(minibatches)
        objective = 0

        self.optimizer.zero_grad()
        for p in self.network.parameters():
            if p.grad is None:
                p.grad = torch.zeros_like(p)

        for (xi, yi), (xj, yj) in random_pairs_of_minibatches(minibatches):
            # fine tune clone-network on task "i"
            inner_net = copy.deepcopy(self.network)

            inner_opt = get_optimizer(
                self.hparams["optimizer"],
                #  "SGD",
                inner_net.parameters(),
                lr=self.hparams["lr"],
                weight_decay=self.hparams["weight_decay"],
            )

            inner_obj = F.cross_entropy(inner_net(xi), yi)

            inner_opt.zero_grad()
            inner_obj.backward()
            inner_opt.step()

            # 1. Compute supervised loss for meta-train set
            # The network has now accumulated gradients Gi
            # The clone-network has now parameters P - lr * Gi
            for p_tgt, p_src in zip(self.network.parameters(), inner_net.parameters()):
                if p_src.grad is not None:
                    p_tgt.grad.data.add_(p_src.grad.data / num_mb)

            # `objective` is populated for reporting purposes
            objective += inner_obj.item()

            # 2. Compute meta loss for meta-val set
            # this computes Gj on the clone-network
            loss_inner_j = F.cross_entropy(inner_net(xj), yj)
            grad_inner_j = autograd.grad(loss_inner_j, inner_net.parameters(), allow_unused=True)

            # `objective` is populated for reporting purposes
            objective += (self.hparams["mldg_beta"] * loss_inner_j).item()

            for p, g_j in zip(self.network.parameters(), grad_inner_j):
                if g_j is not None:
                    p.grad.data.add_(self.hparams["mldg_beta"] * g_j.data / num_mb)

            # The network has now accumulated gradients Gi + beta * Gj
            # Repeat for all train-test splits, do .step()

        objective /= len(minibatches)

        self.optimizer.step()

        return {"loss": objective}

class AbstractMMD(ERM):
    """
    Perform ERM while matching the pair-wise domain feature distributions
    using MMD (abstract class)
    """

    def __init__(self, input_shape, num_classes, num_domains, hparams, gaussian):
        super(AbstractMMD, self).__init__(input_shape, num_classes, num_domains, hparams)
        if gaussian:
            self.kernel_type = "gaussian"
        else:
            self.kernel_type = "mean_cov"

    def my_cdist(self, x1, x2):
        x1_norm = x1.pow(2).sum(dim=-1, keepdim=True)
        x2_norm = x2.pow(2).sum(dim=-1, keepdim=True)
        res = torch.addmm(x2_norm.transpose(-2, -1), x1, x2.transpose(-2, -1), alpha=-2).add_(
            x1_norm
        )
        return res.clamp_min_(1e-30)

    def gaussian_kernel(self, x, y, gamma=(0.001, 0.01, 0.1, 1, 10, 100, 1000)):
        D = self.my_cdist(x, y)
        K = torch.zeros_like(D)

        for g in gamma:
            K.add_(torch.exp(D.mul(-g)))

        return K

    def mmd(self, x, y):
        if self.kernel_type == "gaussian":
            Kxx = self.gaussian_kernel(x, x).mean()
            Kyy = self.gaussian_kernel(y, y).mean()
            Kxy = self.gaussian_kernel(x, y).mean()
            return Kxx + Kyy - 2 * Kxy
        else:
            mean_x = x.mean(0, keepdim=True)
            mean_y = y.mean(0, keepdim=True)
            cent_x = x - mean_x
            cent_y = y - mean_y
            cova_x = (cent_x.t() @ cent_x) / (len(x) - 1)
            cova_y = (cent_y.t() @ cent_y) / (len(y) - 1)

            mean_diff = (mean_x - mean_y).pow(2).mean()
            cova_diff = (cova_x - cova_y).pow(2).mean()

            return mean_diff + cova_diff

    def update(self, x, y, **kwargs):
        minibatches = to_minibatch(x, y)
        objective = 0
        penalty = 0
        nmb = len(minibatches)

        features = [self.featurizer(xi) for xi, _ in minibatches]
        classifs = [self.classifier(fi) for fi in features]
        targets = [yi for _, yi in minibatches]

        for i in range(nmb):
            objective += F.cross_entropy(classifs[i], targets[i])
            for j in range(i + 1, nmb):
                penalty += self.mmd(features[i], features[j])

        objective /= nmb
        if nmb > 1:
            penalty /= nmb * (nmb - 1) / 2

        self.optimizer.zero_grad()
        (objective + (self.hparams["mmd_gamma"] * penalty)).backward()
        self.optimizer.step()

        if torch.is_tensor(penalty):
            penalty = penalty.item()

        return {"loss": objective.item(), "penalty": penalty}

class MMD(AbstractMMD):
    """
    MMD using Gaussian kernel
    """

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(MMD, self).__init__(input_shape, num_classes, num_domains, hparams, gaussian=True)

class CORAL(AbstractMMD):
    """
    MMD using mean and covariance difference
    """

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(CORAL, self).__init__(input_shape, num_classes, num_domains, hparams, gaussian=False)

    def update(self, x, y, **kwargs):
        minibatches = to_minibatch(x, y)
        objective = 0
        penalty = 0
        nmb = len(minibatches)

        # Forward pass
        features = [self.featurizer(xi) for xi, _ in minibatches]
        classifs = [self.classifier(fi) for fi in features]
        targets = [yi for _, yi in minibatches]

        # Compute ERM loss and MMD penalty
        for i in range(nmb):
            objective += F.cross_entropy(classifs[i], targets[i])
            for j in range(i + 1, nmb):
                penalty += self.mmd(features[i], features[j])

        objective /= nmb
        if nmb > 1:
            penalty /= nmb * (nmb - 1) / 2

        # Compute total loss
        total_loss = objective + (self.hparams["mmd_gamma"] * penalty)

        # Backward pass
        self.optimizer.zero_grad()
        total_loss.backward()

        # Compute gradients
        grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])

        # Compute cumulative gradient change
        grads_list = []
        for p in self.network.parameters():
            if p.grad is None:
                grads_list.append(torch.zeros_like(p).view(-1))
            else:
                grads_list.append(p.grad.view(-1))
        grads_2 = torch.cat(grads_list)

        if self.pre_grads is None:
            g_change = torch.zeros_like(grads_2)
        else:
            g_change = torch.abs(grads_2 - self.pre_grads)

        if self.cumulative_g_change is None:
            self.cumulative_g_change = g_change.clone()
        else:
            self.cumulative_g_change += g_change

        self.pre_grads = grads_2.clone()

        # Perform optimization step
        self.optimizer.step()

        # Return required values
        return {
            "loss": objective.item(),
            "penalty": penalty.item() if torch.is_tensor(penalty) else penalty,
            "grads": grads,
            "cumulative_g_change": self.cumulative_g_change,
        }

class MTL(Algorithm):
    """
    A neural network version of
    Domain Generalization by Marginal Transfer Learning
    (https://arxiv.org/abs/1711.07910)
    """

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(MTL, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs * 2, num_classes)
        self.optimizer = get_optimizer(
            hparams["optimizer"],
            list(self.featurizer.parameters()) + list(self.classifier.parameters()),
            lr=self.hparams["lr"],
            weight_decay=self.hparams["weight_decay"],
        )

        self.register_buffer("embeddings", torch.zeros(num_domains, self.featurizer.n_outputs))

        self.ema = self.hparams["mtl_ema"]

    def update(self, x, y, **kwargs):
        minibatches = to_minibatch(x, y)
        loss = 0
        for env, (x, y) in enumerate(minibatches):
            loss += F.cross_entropy(self.predict(x, env), y)

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        return {"loss": loss.item()}

    def update_embeddings_(self, features, env=None):
        return_embedding = features.mean(0)

        if env is not None:
            return_embedding = self.ema * return_embedding + (1 - self.ema) * self.embeddings[env]

            self.embeddings[env] = return_embedding.clone().detach()

        return return_embedding.view(1, -1).repeat(len(features), 1)

    def predict(self, x, env=None):
        features = self.featurizer(x)
        embedding = self.update_embeddings_(features, env).normal_()
        return self.classifier(torch.cat((features, embedding), 1))

class SagNet(Algorithm):
    """
    Style Agnostic Network
    Algorithm 1 from: https://arxiv.org/abs/1910.11645
    """

    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(SagNet, self).__init__(input_shape, num_classes, num_domains, hparams)
        # featurizer network
        self.network_f = networks.Featurizer(input_shape, self.hparams)
        # content network
        self.network_c = nn.Linear(self.network_f.n_outputs, num_classes)
        # style network
        self.network_s = nn.Linear(self.network_f.n_outputs, num_classes)

        # # This commented block of code implements something closer to the
        # # original paper, but is specific to ResNet and puts in disadvantage
        # # the other algorithms.
        # resnet_c = networks.Featurizer(input_shape, self.hparams)
        # resnet_s = networks.Featurizer(input_shape, self.hparams)
        # # featurizer network
        # self.network_f = torch.nn.Sequential(
        #         resnet_c.network.conv1,
        #         resnet_c.network.bn1,
        #         resnet_c.network.relu,
        #         resnet_c.network.maxpool,
        #         resnet_c.network.layer1,
        #         resnet_c.network.layer2,
        #         resnet_c.network.layer3)
        # # content network
        # self.network_c = torch.nn.Sequential(
        #         resnet_c.network.layer4,
        #         resnet_c.network.avgpool,
        #         networks.Flatten(),
        #         resnet_c.network.fc)
        # # style network
        # self.network_s = torch.nn.Sequential(
        #         resnet_s.network.layer4,
        #         resnet_s.network.avgpool,
        #         networks.Flatten(),
        #         resnet_s.network.fc)

        def opt(p):
            return get_optimizer(
                hparams["optimizer"], p, lr=hparams["lr"], weight_decay=hparams["weight_decay"]
            )

        self.optimizer_f = opt(self.network_f.parameters())
        self.optimizer_c = opt(self.network_c.parameters())
        self.optimizer_s = opt(self.network_s.parameters())
        self.weight_adv = hparams["sag_w_adv"]

    def forward_c(self, x):
        # learning content network on randomized style
        return self.network_c(self.randomize(self.network_f(x), "style"))

    def forward_s(self, x):
        # learning style network on randomized content
        return self.network_s(self.randomize(self.network_f(x), "content"))

    def randomize(self, x, what="style", eps=1e-5):
        sizes = x.size()
        alpha = torch.rand(sizes[0], 1).cuda()

        if len(sizes) == 4:
            x = x.view(sizes[0], sizes[1], -1)
            alpha = alpha.unsqueeze(-1)

        mean = x.mean(-1, keepdim=True)
        var = x.var(-1, keepdim=True)

        x = (x - mean) / (var + eps).sqrt()

        idx_swap = torch.randperm(sizes[0])
        if what == "style":
            mean = alpha * mean + (1 - alpha) * mean[idx_swap]
            var = alpha * var + (1 - alpha) * var[idx_swap]
        else:
            x = x[idx_swap].detach()

        x = x * (var + eps).sqrt() + mean
        return x.view(*sizes)

    def update(self, x, y, **kwargs):
        all_x = torch.cat([xi for xi in x])
        all_y = torch.cat([yi for yi in y])

        # learn content
        self.optimizer_f.zero_grad()
        self.optimizer_c.zero_grad()
        loss_c = F.cross_entropy(self.forward_c(all_x), all_y)
        loss_c.backward()
        self.optimizer_f.step()
        self.optimizer_c.step()

        # learn style
        self.optimizer_s.zero_grad()
        loss_s = F.cross_entropy(self.forward_s(all_x), all_y)
        loss_s.backward()
        self.optimizer_s.step()

        # learn adversary
        self.optimizer_f.zero_grad()
        loss_adv = -F.log_softmax(self.forward_s(all_x), dim=1).mean(1).mean()
        loss_adv = loss_adv * self.weight_adv
        loss_adv.backward()
        self.optimizer_f.step()

        return {
            "loss_c": loss_c.item(),
            "loss_s": loss_s.item(),
            "loss_adv": loss_adv.item(),
        }

    def predict(self, x):
        return self.network_c(self.network_f(x))

class RSC(ERM):
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(RSC, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.drop_f = (1 - hparams["rsc_f_drop_factor"]) * 100
        self.drop_b = (1 - hparams["rsc_b_drop_factor"]) * 100
        self.num_classes = num_classes
        self.pre_grads = None
        self.cumulative_g_change = None

    def update(self, x, y, **kwargs):
        # inputs
        all_x = torch.cat([xi for xi in x])
        # labels
        all_y = torch.cat([yi for yi in y])
        # one-hot labels
        all_o = torch.nn.functional.one_hot(all_y, self.num_classes)
        # features
        all_f = self.featurizer(all_x)
        # predictions
        all_p = self.classifier(all_f)

        # Equation (1): compute gradients with respect to representation
        all_g = autograd.grad((all_p * all_o).sum(), all_f)[0]

        # Equation (2): compute top-gradient-percentile mask
        percentiles = np.percentile(all_g.cpu(), self.drop_f, axis=1)
        percentiles = torch.Tensor(percentiles)
        percentiles = percentiles.unsqueeze(1).repeat(1, all_g.size(1))
        mask_f = all_g.lt(percentiles.cuda()).float()

        # Equation (3): mute top-gradient-percentile activations
        all_f_muted = all_f * mask_f

        # Equation (4): compute muted predictions
        all_p_muted = self.classifier(all_f_muted)

        # Section 3.3: Batch Percentage
        all_s = F.softmax(all_p, dim=1)
        all_s_muted = F.softmax(all_p_muted, dim=1)
        changes = (all_s * all_o).sum(1) - (all_s_muted * all_o).sum(1)
        percentile = np.percentile(changes.detach().cpu(), self.drop_b)
        mask_b = changes.lt(percentile).float().view(-1, 1)
        mask = torch.logical_or(mask_f, mask_b).float()

        # Equations (3) and (4) again, this time mutting over examples
        all_p_muted_again = self.classifier(all_f * mask)

        # Equation (5): update
        loss = F.cross_entropy(all_p_muted_again, all_y)
        self.optimizer.zero_grad()
        loss.backward()

        grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])

        grads_list = []
        for p in self.network.parameters():
            if p.grad is None:
                grads_list.append(torch.zeros_like(p).view(-1))
            else:
                grads_list.append(p.grad.view(-1))
        grads_2 = torch.cat(grads_list)

        if self.pre_grads is None:
            g_change = torch.zeros_like(grads_2)
        else:
            g_change = torch.abs(grads_2 - self.pre_grads)

        if self.cumulative_g_change is None:
            self.cumulative_g_change = g_change.clone()
        else:
            self.cumulative_g_change += g_change

        self.pre_grads = grads_2.clone()

        self.optimizer.step()
        return {"loss": loss.item(), "grads": grads, "cumulative_g_change" : self.cumulative_g_change}
    


    #내 메소드---------------------------------------------------

#후보 메소드--------------------------------------
class gsnr1106(Algorithm):
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(gsnr1106, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)

        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(), 
            lr=self.hparams["lr"], 
            momentum=self.hparams["momentum"], 
            weight_decay=self.hparams['weight_decay'], 
            nesterov=True
            )
        
        self.prev_state = None
        self.gmean = None
        self.ge2 = None
        self.scale = None
    
    def get_current_state(self):
        return {name: param.clone().detach() for name, param in self.network.named_parameters()}

    def set_state(self, new_state):
        for name, param in self.network.named_parameters():
            param.data.copy_(new_state[name])
            
    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)

        self.optimizer.zero_grad()
        loss.backward()

        grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])
        current_state = self.get_current_state()

        if self.prev_state is None:
            self._initialize_preconditioning(current_state)

        self._update_preconditioning(
            convergence_rate=self.hparams['convergence_rate'], 
            moving_avg=self.hparams['moving_avg'] 
        )
        
        self.set_state(self.prev_state)
        return {"loss": loss.item(), 'grads': grads}

    def _initialize_preconditioning(self, current_state):
        self.prev_state = current_state
        self.gmean = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.ge2 = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.scale = 0.0

    def _update_preconditioning(self, convergence_rate, moving_avg):
        gsnr_values = []
        grad_sgd = {}
        pgrad = {}
        gsnr = {}
        pGsnr = {}

        self.scale = (moving_avg * self.scale + 1.0)
        scale1 = (1 - moving_avg) * self.scale
        scale2 = 2.0 - scale1
        rho = (1.0 - moving_avg) * scale2 / (1.0 + moving_avg) / scale1

        with torch.no_grad():
            for k, param in self.network.named_parameters():
                delta = (param.grad.data).detach()
                self.gmean[k] = self.gmean[k] * moving_avg + delta * (1.0 - moving_avg)
                self.ge2[k] = self.ge2[k] * moving_avg + (delta ** 2) * (1.0 - moving_avg)
                gm = self.gmean[k] / scale1
                ge2 = self.ge2[k] / scale1
                var = ge2 - gm.square()
                var /= (1.0 - rho)
                var = torch.where(var > 0.0, var, (torch.zeros_like(var) + 1e-8))
                invvar = torch.clamp(1 / var, min=0.0, max=10.0)
                mvar = rho * var
                mvar = torch.where(mvar > 0.0, mvar, (torch.zeros_like(mvar) + 1e-8))
                gsnr[k] = (gm.square() / var).add(1 / self.scale)

                numerator = 1.0
                denominator = 1.0 + mvar / (gm.square() + 1e-8)
                pGsnr[k] = (numerator / denominator) * torch.tanh(invvar)

                noise_scale = torch.sum(torch.tanh(invvar) * torch.abs(gm) * (numerator / denominator)) / torch.sum(torch.tanh(invvar))
                noise = torch.normal(torch.zeros_like(delta), torch.ones_like(delta))
                noise = noise * noise_scale
                grad_sgd[k] = (1 - torch.tanh(invvar)) * noise

            for k, param in self.network.named_parameters():
                pgrad[k] = self.gmean[k] / scale1 * pGsnr[k].view_as(param)

            for k, param in self.network.named_parameters():
                self.prev_state[k] -= (pgrad[k] + grad_sgd[k]) * convergence_rate

    def predict(self, x):
        return self.network(x)

class prob1106(Algorithm):
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(prob1106, self).__init__(input_shape, num_classes, num_domains, hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = nn.Linear(self.featurizer.n_outputs, num_classes)
        self.network = nn.Sequential(self.featurizer, self.classifier)

        self.optimizer = get_optimizer(
            hparams["optimizer"],
            self.network.parameters(), 
            lr=self.hparams["lr"], 
            momentum=self.hparams["momentum"], 
            weight_decay=self.hparams['weight_decay'], 
            nesterov=True
            )
        
        self.prev_state = None
        self.gmean = None
        self.ge2 = None
        self.scale = None
    
    def get_current_state(self):
        return {name: param.clone().detach() for name, param in self.network.named_parameters()}

    def set_state(self, new_state):
        for name, param in self.network.named_parameters():
            param.data.copy_(new_state[name])
            
    def update(self, x, y, **kwargs):
        all_x = torch.cat(x)
        all_y = torch.cat(y)
        loss = F.cross_entropy(self.predict(all_x), all_y)

        self.optimizer.zero_grad()
        loss.backward()
        grads = torch.cat([param.grad.view(-1) for param in self.network.parameters() if param.grad is not None])
        current_state = self.get_current_state()

        if self.prev_state is None:
            self._initialize_preconditioning(current_state)
        self._update_preconditioning(
            convergence_rate=self.hparams['convergence_rate'], 
            moving_avg=self.hparams['moving_avg'] 
        )
        self.set_state(self.prev_state)
        return {"loss": loss.item(), 'grads': grads}

    def _initialize_preconditioning(self, current_state):
        self.prev_state = current_state
        self.gmean = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.ge2 = {k: torch.zeros_like(param) for k, param in self.network.named_parameters()}
        self.scale = 0.0

    def _update_preconditioning(self, convergence_rate, moving_avg):
        gsnr_values = []
        grad_sgd = {}
        pgrad = {}
        gsnr = {}
        pGsnr = {}

        self.scale = (moving_avg * self.scale + 1.0)
        scale1 = (1 - moving_avg) * self.scale
        scale2 = 2.0 - scale1
        rho = (1.0 - moving_avg) * scale2 / (1.0 + moving_avg) / scale1

        with torch.no_grad():
            for k, param in self.network.named_parameters():
                delta = (param.grad.data).detach()
                self.gmean[k] = self.gmean[k] * moving_avg + delta * (1.0 - moving_avg)
                self.ge2[k] = self.ge2[k] * moving_avg + (delta ** 2) * (1.0 - moving_avg)
                gm = self.gmean[k] / scale1
                ge2 = self.ge2[k] / scale1
                var = ge2 - gm.square()
                var /= (1.0 - rho)
                var = torch.where(var > 0.0, var, (torch.zeros_like(var) + 1e-8))

                mvar = rho * var
                mvar = torch.where(mvar > 0.0, mvar, (torch.zeros_like(mvar) + 1e-8))
                
                gsnr[k] = (gm.square() / var).add(1 / self.scale)

                normal_dist = Normal(gm, torch.sqrt(mvar))
                prob = normal_dist.cdf(torch.zeros_like(param.data))
                prob = torch.abs(2 * prob - 1.0)

                numerator = 1.0
                denominator = 1.0 +  mvar/(gm.square()+1e-8)

                pGsnr[k] = (numerator/denominator) * prob

                noise_scale = torch.sum(prob * torch.abs(gm)/denominator) / torch.sum(prob)
                noise = torch.normal(torch.zeros_like(delta),torch.ones_like(delta))
                noise = noise * noise_scale

                grad_sgd[k] = (1 - prob) * noise

            for k, param in self.network.named_parameters():
                pgrad[k] = self.gmean[k] / scale1 * pGsnr[k].view_as(param)

            for k, param in self.network.named_parameters():
                self.prev_state[k] -= (pgrad[k] + grad_sgd[k]) * convergence_rate

    def predict(self, x):
        return self.network(x)
