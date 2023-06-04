import os
import math
import sys

import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as Func
from torch.nn import init
from torch.nn.parameter import Parameter
from torch.nn.modules.module import Module

import torch.optim as optim


class spatial_attn(nn.Module):
    def __init__(self, num_peds):
        super(spatial_attn).__init__()
        
        self.W = nn.Linear(2*num_peds, 1)
        self.lrelu = nn.LeakyReLU(0.2) 
        self.softmax = nn.Softmax(dim=1)

    def forward(self, A):
        '''
        A: (seq_len, num_peds, num_peds)
        '''
        num_peds = A.shape[2]
        new_adjs = []
        for a_ in A:
            
            a_ = a_.T
            a_rep1 = a_.repeat_interleave(num_peds, 0)
            a_rep2 = a_.repeat(num_peds, 1, 1).view(-1, num_peds)
            a_concat = torch.cat([a_rep1, a_rep2], dim=1).view(num_peds,num_peds,2*num_peds)
            e = self.lrelu(self.W(a_concat))
            alpha = self.softmax(e)
            attn_res = torch.einsum('ijh,jhf->ihf', alpha, a_)
            new_adjs.append(attn_res)
        
        return torch.stack(new_adjs, dim=0)

        
class ConvTemporalGraphical(nn.Module):
    #Source : https://github.com/yysijie/st-gcn/blob/master/net/st_gcn.py
    r"""The basic module for applying a graph convolution.
    Args:
        in_channels (int): Number of channels in the input sequence data
        out_channels (int): Number of channels produced by the convolution
        kernel_size (int): Size of the graph convolving kernel
        t_kernel_size (int): Size of the temporal convolving kernel
        t_stride (int, optional): Stride of the temporal convolution. Default: 1
        t_padding (int, optional): Temporal zero-padding added to both sides of
            the input. Default: 0
        t_dilation (int, optional): Spacing between temporal kernel elements.
            Default: 1
        bias (bool, optional): If ``True``, adds a learnable bias to the output.
            Default: ``True``
    Shape:
        - Input[0]: Input graph sequence in :math:`(N, in_channels, T_{in}, V)` format 
        - Input[1]: Input graph adjacency matrix in :math:`(K, V, V)` format 
        - Output[0]: Outpu graph sequence in :math:`(N, out_channels, T_{out}, V)` format
        - Output[1]: Graph adjacency matrix for output data in :math:`(K, V, V)` format
        where
            :math:`N` is a batch size,
            :math:`K` is the spatial kernel size, as :math:`K == kernel_size[1]`,
            :math:`T_{in}/T_{out}` is a length of input/output sequence,
            :math:`V` is the number of graph nodes. 
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 t_kernel_size=1,
                 t_stride=1,
                 t_padding=0,
                 t_dilation=1,
                 bias=True):
        super(ConvTemporalGraphical,self).__init__()
        self.kernel_size = kernel_size
        self.conv = nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=(t_kernel_size, 1),
            padding=(t_padding, 0),
            stride=(t_stride, 1),
            dilation=(t_dilation, 1),
            bias=bias)

    def forward(self, x, A):
        assert A.size(0) == self.kernel_size
        x = self.conv(x)
        x = torch.einsum('nctv,tvw->nctw', (x, A))  
        return x.contiguous(), A
    

class st_gcn(nn.Module):
    r"""Applies a spatial temporal graph convolution over an input graph sequence.
    Args:
        in_channels (int): Number of channels in the input sequence data
        out_channels (int): Number of channels produced by the convolution
        kernel_size (tuple): Size of the temporal convolving kernel and graph convolving kernel
        stride (int, optional): Stride of the temporal convolution. Default: 1
        dropout (int, optional): Dropout rate of the final output. Default: 0
        residual (bool, optional): If ``True``, applies a residual mechanism. Default: ``True``
    Shape:
        - Input[0]: Input graph sequence in :math:`(N, in_channels, T_{in}, V)` format
        - Input[1]: Input graph adjacency matrix in :math:`(K, V, V)` format
        - Output[0]: Outpu graph sequence in :math:`(N, out_channels, T_{out}, V)` format
        - Output[1]: Graph adjacency matrix for output data in :math:`(K, V, V)` format
        where
            :math:`N` is a batch size,
            :math:`K` is the spatial kernel size, as :math:`K == kernel_size[1]`,
            :math:`T_{in}/T_{out}` is a length of input/output sequence,
            :math:`V` is the number of graph nodes.
    """

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 use_mdn = False,
                 stride=1,
                 dropout=0,
                 residual=True):
        super(st_gcn,self).__init__()
        
#         print("outstg",out_channels)

        assert len(kernel_size) == 2
        assert kernel_size[0] % 2 == 1
        padding = ((kernel_size[0] - 1) // 2, 0)
        self.use_mdn = use_mdn

        self.gcn = ConvTemporalGraphical(in_channels, out_channels,
                                         kernel_size[1])
        

        self.tcn = nn.Sequential(
            nn.BatchNorm2d(out_channels),
            nn.PReLU(),
            nn.Conv2d(
                out_channels,
                out_channels,
                (kernel_size[0], 1),
                (stride, 1),
                padding,
            ),
            nn.BatchNorm2d(out_channels),
            nn.Dropout(dropout, inplace=True),
        )

        if not residual:
            self.residual = lambda x: 0

        elif (in_channels == out_channels) and (stride == 1):
            self.residual = lambda x: x

        else:
            self.residual = nn.Sequential(
                nn.Conv2d(
                    in_channels,
                    out_channels,
                    kernel_size=1,
                    stride=(stride, 1)),
                nn.BatchNorm2d(out_channels),
            )

        self.prelu = nn.PReLU()

    def forward(self, x, A):

        res = self.residual(x)
        x, A = self.gcn(x, A)

        x = self.tcn(x) + res
        
        if not self.use_mdn:
            x = self.prelu(x)

        return x, A

class T_GNN(nn.Module):
    def __init__(self,
                 n_stgcnn=1,
                 n_txpcnn=1,
                 input_feat=2,
                 feat_dim=64,
                 output_feat=5,
                 seq_len=8,
                 pred_seq_len=12,
                 kernel_size=3
                 ):
        super(T_GNN,self).__init__()
        self.n_stgcnn= n_stgcnn
        self.n_txpcnn = n_txpcnn


        self.lin_proj = nn.Linear(input_feat, feat_dim)
        self.relu = nn.ReLU()

        self.st_gcns = nn.ModuleList()
        self.st_gcns.append(st_gcn(feat_dim,feat_dim,(kernel_size,seq_len)))
        for j in range(1,self.n_stgcnn-1):
            self.st_gcns.append(st_gcn(feat_dim,feat_dim,(kernel_size,seq_len)))
        self.st_gcns.append(st_gcn(feat_dim,output_feat,(kernel_size,seq_len)))


        self.tpcnns = nn.ModuleList()
        self.tpcnns.append(nn.Conv2d(seq_len,pred_seq_len,3,padding=1))
        for j in range(1,self.n_txpcnn):
            self.tpcnns.append(nn.Conv2d(pred_seq_len,pred_seq_len,3,padding=1))
            
            
        self.prelus = nn.ModuleList()
        for j in range(self.n_txpcnn):
            self.prelus.append(nn.PReLU())

    def forward(self,v,a):
        # v: (1, T_obs, num_peds, feat = 2) -> (1, 8, num_peds, 2)
        v = self.relu(self.lin_proj(v)).permute(0,3,1,2)

        # v: (1, feat = 64, T_obs, num_peds) -> (1, 64, 8, num_peds)
        for k in range(len(self.st_gcns)):
            v,a = self.st_gcns[k](v,a)

        # v: (1, feat = 5, T_obs, num_peds) -> (1, 5, 8, num_peds)
        v = v.view(v.shape[0],v.shape[2],v.shape[1],v.shape[3])
        # v: (1, T_obs, feat=5, num_peds) -> (1, 8, 5, num_peds) 
        # The reason for the reshape is, the TCNN module consider the temporal axis as the feature dimension.

        v = self.prelus[0](self.tpcnns[0](v))

        for k in range(1,self.n_txpcnn-1):
            v =  self.prelus[k](self.tpcnns[k](v)) + v
            
        v = v.view(v.shape[0],v.shape[2],v.shape[1],v.shape[3])
        
        return v,a # v: (1, out_feat, T_pred, num_peds) -> (1, 5, 12, num_peds), a has the same shape
