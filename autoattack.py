import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
import argparse
import time

class AutoAttack():
    def __init__(self, model, norm='Linf', eps=.3, seed=None, verbose=True,
                 attacks_to_run=['apgd-ce', 'apgd-dlr', 'fab', 'square'],
                 plus=False, is_tf_model=False, device='cuda'):
        self.model = model
        self.norm = norm
        assert norm in ['Linf', 'L2']
        self.epsilon = eps
        self.seed = seed
        self.verbose = verbose
        self.attacks_to_run = attacks_to_run if not plus else attacks_to_run.extend(['apgd-t', 'fab-t'])
        self.plus = plus
        self.is_tf_model = is_tf_model
        self.device = device
        
        if not self.is_tf_model:
            from autopgd_pt import APGDAttack
            self.apgd = APGDAttack(self.model, n_restarts=5, n_iter=100, verbose=False,
                eps=self.epsilon, norm=self.norm, eot_iter=1, rho=.75, seed=self.seed)
            
            from fab_pt import FABAttack
            self.fab = FABAttack(self.model, n_restarts=5, n_iter=100, eps=self.epsilon, seed=self.seed,
                norm=self.norm, verbose=False, device=self.device)
        
            from square import SquareAttack
            self.square = SquareAttack(self.model, p_init=.8, n_queries=5000, eps=self.epsilon, norm=self.norm,
                early_stop=True, n_restarts=1, seed=self.seed, verbose=False, device=self.device)
                
            from autopgd_pt import APGDAttack_targeted
            self.apgd_targeted = APGDAttack_targeted(self.model, n_restarts=1, n_iter=100, verbose=False,
                eps=self.epsilon, norm=self.norm, eot_iter=1, rho=.75, seed=self.seed)
    
        else:
            from autopgd_tf import APGDAttack
            self.apgd = APGDAttack(self.model, n_restarts=5, n_iter=100, verbose=False,
                eps=self.epsilon, norm=self.norm, eot_iter=1, rho=.75, seed=self.seed)
            
            from fab_tf import FABAttack
            self.fab = FABAttack(self.model, n_restarts=5, n_iter=100, eps=self.epsilon, seed=self.seed,
                norm=self.norm, verbose=False, device=self.device)
        
            from square import SquareAttack
            self.square = SquareAttack(self.model.predict, p_init=.8, n_queries=5000, eps=self.epsilon, norm=self.norm,
                early_stop=True, n_restarts=1, seed=self.seed, verbose=False, device=self.device)
                
            from autopgd_tf import APGDAttack_targeted
            self.apgd_targeted = APGDAttack_targeted(self.model, n_restarts=1, n_iter=100, verbose=False,
                eps=self.epsilon, norm=self.norm, eot_iter=1, rho=.75, seed=self.seed)
    
        ### TODO: add device to autopgd
    
    def get_logits(self, x):
        if not self.is_tf_model:
            return self.model(x)
        else:
            return self.model.predict(x)
    
    def get_seed(self):
        return time.time() if self.seed is None else self.seed
    
    def run_standard_evaluation(self, x_orig, y_orig, bs=250):
        # update attacks list if plus activated or deactivated after initialization
        if self.plus:
            if not 'apgd-t' in self.attacks_to_run:
                self.attacks_to_run.extend(['apgd-t'])
            if not 'fab-t' in self.attacks_to_run:
                self.attacks_to_run.extend(['fab-t'])
        else:
            if 'apgd-t' in self.attacks_to_run:
                self.attacks_to_run.remove('apgd-t')
            if 'fab-t' in self.attacks_to_run:
                self.attacks_to_run.remove('fab-t')
        
        with torch.no_grad():
            # calculate accuracy
            n_batches = int(np.ceil(x_orig.shape[0] / bs))
            robust_flags = torch.zeros(x_orig.shape[0], dtype=torch.bool, device=x_orig.device)
            for batch_idx in range(n_batches):
                start_idx = batch_idx * bs
                end_idx = min( (batch_idx + 1) * bs, x_orig.shape[0])

                x = x_orig[start_idx:end_idx, :].clone().to(self.device)
                y = y_orig[start_idx:end_idx].clone().to(self.device)
                output = self.get_logits(x)
                correct_batch = y.eq(output.max(dim=1)[1])
                robust_flags[start_idx:end_idx] = correct_batch.detach().to(robust_flags.device)

            robust_accuracy = torch.sum(robust_flags).item() / x_orig.shape[0]
                
            if self.verbose:
                print('initial accuracy: {:.2%}'.format(robust_accuracy))
                    
            x_adv = x_orig.clone().detach()
            startt = time.time()
            for attack in self.attacks_to_run:
                # item() is super important as pytorch int division uses floor rounding
                num_robust = torch.sum(robust_flags).item()

                if num_robust == 0:
                    break

                n_batches = int(np.ceil(num_robust / bs))

                robust_lin_idcs = torch.nonzero(robust_flags, as_tuple=False)
                if num_robust > 1:
                    robust_lin_idcs.squeeze_()
                
                for batch_idx in range(n_batches):
                    start_idx = batch_idx * bs
                    end_idx = min((batch_idx + 1) * bs, num_robust)

                    batch_datapoint_idcs = robust_lin_idcs[start_idx:end_idx]
                    x = x_orig[batch_datapoint_idcs, :].clone().to(self.device)
                    y = y_orig[batch_datapoint_idcs].clone().to(self.device)

                    # make sure that x is a 4d tensor even if there is only a single datapoint left
                    if len(x.shape) == 3:
                        x.unsqueeze_(dim=0)
                
                    # run attack
                    if attack == 'apgd-ce':
                        # apgd on cross-entropy loss
                        self.apgd.loss = 'ce'
                        self.apgd.seed = self.get_seed()
                        _, adv_curr = self.apgd.perturb(x, y, cheap=True)
                    
                    elif attack == 'apgd-dlr':
                        # apgd on dlr loss
                        self.apgd.loss = 'dlr'
                        self.apgd.seed = self.get_seed()
                        _, adv_curr = self.apgd.perturb(x, y, cheap=True)
                    
                    elif attack == 'fab':
                        # fab
                        self.fab.targeted = False
                        self.fab.seed = self.get_seed()
                        adv_curr = self.fab.perturb(x, y)
                    
                    elif attack == 'square':
                        # square
                        self.square.seed = self.get_seed()
                        _, adv_curr = self.square.perturb(x, y)
                    
                    elif attack == 'apgd-t':
                        # targeted apgd
                        self.apgd_targeted.seed = self.get_seed()
                        _, adv_curr = self.apgd_targeted.perturb(x, y, cheap=True)
                    
                    elif attack == 'fab-t':
                        # fab targeted
                        self.fab.targeted = True
                        self.fab.seed = self.get_seed()
                        adv_curr = self.fab.perturb(x, y)
                    
                    else:
                        raise ValueError('Attack not supported')
                
                    output = self.get_logits(adv_curr)
                    false_batch = ~y.eq(output.max(dim=1)[1]).to(robust_flags.device)
                    non_robust_lin_idcs = batch_datapoint_idcs[false_batch]
                    robust_flags[non_robust_lin_idcs] = False

                    x_adv[non_robust_lin_idcs] = adv_curr[false_batch].detach().to(x_adv.device)
                
                    if self.verbose:
                        num_non_robust_batch = torch.sum(false_batch)    
                        print('{} - {}/{} - {} out of {} successfully perturbed'.format(
                            attack, batch_idx + 1, n_batches, num_non_robust_batch, x.shape[0]))
                
                robust_accuracy = torch.sum(robust_flags).item() / x_orig.shape[0]
                if self.verbose:
                    print('robust accuracy after {}: {:.2%} (total time {:.1f} s)'.format(
                        attack.upper(), robust_accuracy, time.time() - startt))
                    
            # final check
            if self.verbose:
                if self.norm == 'Linf':
                    res = (x_adv - x_orig).abs().view(x_orig.shape[0], -1).max(1)[0]
                elif self.norm == 'L2':
                    res = ((x_adv - x_orig) ** 2).view(x_orig.shape[0], -1).sum(-1).sqrt()
                print('max {} perturbation: {:.5f}, nan in tensor: {}, max: {:.5f}, min: {:.5f}'.format(
                    self.norm, res.max(), (x_adv != x_adv).sum(), x_adv.max(), x_adv.min()))
                print('robust accuracy: {:.2%}'.format(robust_accuracy))
        
        return x_adv
        
    def clean_accuracy(self, x_orig, y_orig, bs=250):
        n_batches = x_orig.shape[0] // bs
        acc = 0.
        for counter in range(n_batches):
            x = x_orig[counter * bs:min((counter + 1) * bs, x_orig.shape[0])].clone().to(self.device)
            y = y_orig[counter * bs:min((counter + 1) * bs, x_orig.shape[0])].clone().to(self.device)
            output = self.get_logits(x)
            acc += (output.max(1)[1] == y).float().sum()
            
        if self.verbose:
            print('clean accuracy: {:.2%}'.format(acc / x_orig.shape[0]))
        
        return acc.item() / x_orig.shape[0]
        
    def run_standard_evaluation_individual(self, x_orig, y_orig, bs=250):
        # update attacks list if plus activated after initialization
        if self.plus:
            if not 'apgd-t' in self.attacks_to_run:
                self.attacks_to_run.extend(['apgd-t'])
            if not 'fab-t' in self.attacks_to_run:
                self.attacks_to_run.extend(['fab-t'])
        
        l_attacks = self.attacks_to_run
        adv = {}
        self.plus = False
        verbose_indiv = self.verbose
        self.verbose = False
        
        for c in l_attacks:
            startt = time.time()
            self.attacks_to_run = [c]
            adv[c] = self.run_standard_evaluation(x_orig, y_orig, bs=bs)
            if verbose_indiv:    
                acc_indiv  = self.clean_accuracy(adv[c], y_orig, bs=bs)
                space = '\t \t' if c == 'fab' else '\t'
                print('robust accuracy by {} {} {:.2%} \t (time attack: {:.1f} s)'.format(
                    c.upper(), space, acc_indiv,  time.time() - startt))
        
        return adv
        
    def cheap(self):
        self.apgd.n_restarts = 1
        self.fab.n_restarts = 1
        self.apgd_targeted.n_restarts = 1
        self.square.n_queries = 1000
        self.plus = False

