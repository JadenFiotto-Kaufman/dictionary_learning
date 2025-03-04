import torch as t
import zstandard as zstd
import json
import io
from nnsight import LanguageModel

"""
Implements a buffer of activations
"""

class ActivationBuffer:
    def __init__(self, 
                 data, # generator which yields text data
                 model:LanguageModel, # LanguageModel from which to extract activations
                 submodule, # submodule of the model from which to extract activations
                 in_feats=None,
                 out_feats=None, 
                 io='out', # can be 'in', 'out', or 'in_to_out'
                 n_ctxs=3e4, # approximate number of contexts to store in the buffer
                 ctx_len=128, # length of each context
                 in_batch_size=512, # size of batches in which to process the data when adding to buffer
                 out_batch_size=8192, # size of batches in which to return activations
                 device='cpu' # device on which to store the activations
                 ):
        
        if io == 'in':
            if in_feats is None:
                try:
                    in_feats = submodule.in_features
                except:
                    raise ValueError("in_feats cannot be inferred and must be specified directly")
            self.activations = t.empty(0, in_feats, device=device)

        elif io == 'out':
            if out_feats is None:
                try:
                    out_feats = submodule.out_features
                except:
                    raise ValueError("out_feats cannot be inferred and must be specified directly")
            self.activations = t.empty(0, out_feats, device=device)
        elif io == 'in_to_out':
            if in_feats is None:
                try:
                    in_feats = submodule.in_features
                except:
                    raise ValueError("in_feats cannot be inferred and must be specified directly")
            if out_feats is None:
                try:
                    out_feats = submodule.out_features
                except:
                    raise ValueError("out_feats cannot be inferred and must be specified directly")
            self.activations_in = t.empty(0, in_feats)
            self.activations_out = t.empty(0, out_feats)
        self.read = t.zeros(0).bool()

        self.data = data
        self.model:LanguageModel = model # assumes nnsight model is already on the device
        self.submodule = submodule
        self.io = io
        self.n_ctxs = n_ctxs
        self.ctx_len = ctx_len
        self.in_batch_size = in_batch_size
        self.out_batch_size = out_batch_size
        self.device = device
    
    def __iter__(self):
        return self

    def __next__(self):
        """
        Return a batch of activations
        """
        with t.no_grad():
            # if buffer is less than half full, refresh
            if (~self.read).sum() < self.n_ctxs * self.ctx_len // 2:
                self.refresh()

            # return a batch
            unreads = (~self.read).nonzero().squeeze()
            idxs = unreads[t.randperm(len(unreads), device=unreads.device)[:self.out_batch_size]]
            self.read[idxs] = True
            if self.io in ['in', 'out']:
                return self.activations[idxs]
            else:
                return (self.activations_in[idxs], self.activations_out[idxs])
    
    def text_batch(self, batch_size=None):
        """
        Return a list of text
        """
        if batch_size is None:
            batch_size = self.in_batch_size
        try:
            return [
                next(self.data) for _ in range(batch_size)
            ]
        except StopIteration:
            raise StopIteration("End of data stream reached")
    
    def tokenized_batch(self, batch_size=None):
        """
        Return a batch of tokenized inputs.
        """
        texts = self.text_batch(batch_size=batch_size)
        return self.model.tokenizer(
            texts,
            return_tensors='pt',
            max_length=self.ctx_len,
            padding=True,
            truncation=True
        )

    def _refresh_std(self):
        """
        For when io == 'in' or 'out'
        """
        self.activations = self.activations[~self.read]

        while len(self.activations) < self.n_ctxs * self.ctx_len:
                
            with t.no_grad(), self.model.trace(self.text_batch(), invoker_args={'truncation': True, 'max_length': self.ctx_len}) as tracer:
                if self.io == 'in':
                    hidden_states = self.submodule.input
                else:
                    hidden_states = self.submodule.output
                while type(hidden_states.shape) == tuple:
                    hidden_states = hidden_states[0]
                hidden_states = hidden_states[tracer._invoker.inputs['attention_mask'] != 0]
                hidden_states = hidden_states.save()
            self.activations = t.cat([self.activations, hidden_states.to(self.device)], dim=0)
            self.read = t.zeros(len(self.activations), dtype=t.bool, device=self.device)

    def _refresh_in_to_out(self):
        """
        For when io == 'in_to_out'
        """
        self.activations_in = self.activations_in[~self.read]
        self.activations_out = self.activations_out[~self.read]

        while len(self.activations_in) < self.n_ctxs * self.ctx_len:

            with t.no_grad(), self.model.trace(self.text_batch(), invoker_args={'truncation': True, 'max_length': self.ctx_len}) as tracer:
                hidden_states_in = self.submodule.input
                hidden_states_out = self.submodule.output
                while type(hidden_states_in.shape) == tuple:
                    hidden_states_in = hidden_states_in[0]
                while type(hidden_states_out.shape) == tuple:
                    hidden_states_out = hidden_states_out[0]
                hidden_states_in = hidden_states_in[tracer._invoker.inputs['attention_mask'] != 0]
                hidden_states_out = hidden_states_out[tracer._invoker.inputs['attention_mask'] != 0]
                hidden_states_in, hidden_states_out = hidden_states_in.save(), hidden_states_out.save()
                
            self.activations_in =  t.cat([self.activations_in,  hidden_states_in.to(self.device)], dim=0)
            self.activations_out = t.cat([self.activations_out, hidden_states_out.to(self.device)], dim=0)
            self.read = t.zeros(len(self.activations_in), dtype=t.bool, device=self.device)

    def refresh(self):
        """
        Refresh the buffer
        """
        # print("refreshing buffer...")

        if self.io == 'in' or self.io == 'out':
            self._refresh_std()
        else:
            self._refresh_in_to_out()

        # print('buffer refreshed...')

    def close(self):
        """
        Close the text stream and the underlying compressed file.
        """
        self.text_stream.close()
