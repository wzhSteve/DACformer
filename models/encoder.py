import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from scipy.fftpack import fft, ifft
import matplotlib.pyplot as plt
from matplotlib.pylab import mpl
from scipy import signal

from models.attn import FullAttention, AttentionLayer
from models.cross_correlation import cross_correlation

class moving_avg(nn.Module):
    """
    Moving average block to highlight the trend of time series
    """
    def __init__(self, kernel_size, stride):
        super(moving_avg, self).__init__()
        self.kernel_size = kernel_size
        self.avg = nn.AvgPool1d(kernel_size=kernel_size, stride=stride, padding=0)

    def forward(self, x):
        # padding on the both ends of time series
        front = x[:, 0:1, :].repeat(1, (self.kernel_size - 1) // 2, 1)
        end = x[:, -1:, :].repeat(1, (self.kernel_size - 1) // 2, 1)
        x = torch.cat([front, x, end], dim=1)
        x = self.avg(x.permute(0, 2, 1))
        x = x.permute(0, 2, 1)
        return x

class series_decomp(nn.Module):
    """
    Series decomposition block
    """
    def __init__(self, kernel_size):
        super(series_decomp, self).__init__()
        self.moving_avg = moving_avg(kernel_size, stride=1)

    def forward(self, x):
        moving_mean = self.moving_avg(x)
        res = x - moving_mean
        return res, moving_mean

class separate_encoder_layer(nn.Module):
    def __init__(self, attention, d_model, dropout=0.1, activation="gelu", separate_factor=2, step=4):
        super(separate_encoder_layer, self).__init__()
        self.step = step
        self.attention = attention
        self.linear = nn.Linear(step, step//separate_factor)
        self.dropout = nn.Dropout(dropout)
        self.activation = F.elu

    def forward(self, x, attn_mask=None):
        new_x, attn = self.attention(x, x, x, attn_mask=attn_mask)
        y = x + self.dropout(new_x)
        y = self.dropout(self.activation(self.linear(y.transpose(-1, -2))))
        y = y.transpose(-1, -2)
        return y, attn


class separate_encoder(nn.Module):
    def __init__(self, step, separate_factor, n_heads, mix, dropout=0.01, activation='gelu', d_model=512):
        super(separate_encoder, self).__init__()
        self.separate_factor = [3, 2] #第一层，二层，三层
        self.step = [24, 8, 4]
        self.layer = 2
        self.encoder_layer_list = nn.ModuleList([])
        self.attention_layer_list = nn.ModuleList([])
        self.encoder_layer_list2 = nn.ModuleList([])
        self.attention_layer_list2 = nn.ModuleList([])
        self.cross = nn.ModuleList([])

        count = 0
        while(count < self.layer):
            self.attention_layer_list.append(AttentionLayer(FullAttention(False, attention_dropout=dropout, output_attention=False), d_model, n_heads, mix=mix))
            self.encoder_layer_list.append(separate_encoder_layer(self.attention_layer_list[count], d_model, dropout=dropout, activation=activation, step=self.step[count],
                                    separate_factor=self.separate_factor[count]))
            self.attention_layer_list2.append(
                AttentionLayer(FullAttention(False, attention_dropout=dropout, output_attention=False), d_model,
                               n_heads, mix=mix))
            self.encoder_layer_list2.append(
                separate_encoder_layer(self.attention_layer_list2[count], d_model, dropout=dropout,
                                       activation=activation, step=self.step[count],
                                       separate_factor=self.separate_factor[count]))
            self.cross.append(cross_correlation(self.step[count+1]))
            count = count + 1
            self.trade_off1 = nn.Parameter(torch.zeros(1, 1, 1))
            self.trade_off2 = nn.Parameter(torch.zeros(1, 1, 1))

    def forward(self, x, attn_mask=None):
        batch_size, sequence_len, d_model = x.shape
        count = 0
        layer_output = [] #各层输出的list
        while(count < self.layer):
            #sequence由本层序列长度 cnt为本层分块数
            cnt = sequence_len//self.step[count] #输入分块长度为self.step 输出分块长度为self.step//separate_factor
            #用于存储局部输出
            output = torch.tensor([]).to(x.device)
            output_div = torch.tensor([]).to(x.device)
            last_output = torch.tensor([]).to(x.device)
            last_output_div = torch.tensor([]).to(x.device)
            for i in range(cnt):
                if i == 0:
                    ii = i * self.step[count]
                    # 更新x_mean, x_div
                    x_ii = x[:, ii:ii + self.step[count], :]
                    x_mean = torch.mean(x_ii, dim=1).view(x_ii.shape[0], 1, x_ii.shape[2])
                    temp_mean = x_mean.repeat(1, x_ii.shape[1], 1)
                    temp_div = x_ii - x_mean
                    next_output, _ = self.encoder_layer_list[count](x_ii, attn_mask=attn_mask)
                    next_output_div, _ = self.encoder_layer_list2[count](temp_div, attn_mask=attn_mask)
                    last_output = next_output
                    last_output_div = next_output_div

                else:
                    ii = i * self.step[count]
                    # 更新x_mean, x_div
                    x_ii = x[:, ii:ii + self.step[count], :]
                    x_mean = torch.mean(x_ii, dim=1).view(x_ii.shape[0], 1, x_ii.shape[2])
                    temp_mean = x_mean.repeat(1, x_ii.shape[1], 1)
                    temp_div = x_ii - x_mean
                    next_output, _ = self.encoder_layer_list[count](temp_mean, attn_mask=attn_mask)
                    next_output_div, _ = self.encoder_layer_list2[count](temp_div, attn_mask=attn_mask)
                    last_output = next_output
                    last_output_div = next_output_div
                    next_output = next_output + self.trade_off1 * self.cross[count](last_output, next_output)
                    next_output_div = next_output_div + self.trade_off2 * self.cross[count](last_output_div, next_output_div)

                output = torch.cat((output, next_output), 1)  # 按sequenc_len这一维度拼接
                output_div = torch.cat((output_div, next_output_div), 1)  # 按sequenc_len这一维度拼接
            #print("encoder: 第{}次离散局部输出,output:[{},{},{}]".format(count,output.shape[0],output.shape[1],output.shape[2]))
            x = output
            sequence_len = output.shape[1]
            layer_output.append(output_div)
            count = count + 1  # 层数
        #output为最终隐藏层z ，layer_output为各层输出的list
        return output, layer_output


