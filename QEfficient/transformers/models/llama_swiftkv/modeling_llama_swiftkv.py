# coding=utf-8
# Adapted from
# https://github.com/huggingface/transformers/blob/v4.28.0/src/transformers/models/llama/modeling_llama.py
# Copyright 2023 The vLLM team.
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Inference-only LLaMA model compatible with HuggingFace weights."""

import math
from typing import List, Optional, Tuple, Union

import torch
from torch import nn
from transformers.cache_utils import Cache, StaticCache
from transformers.modeling_attn_mask_utils import AttentionMaskConverter
from transformers.models.llama.modeling_llama import LlamaMLP, LlamaRMSNorm, logger, repeat_kv

from QEfficient.transformers.cache_utils import QEffDynamicCache
from QEfficient.transformers.modeling_attn_mask_utils import _create_causal_mask
from QEfficient.transformers.models.llama.modeling_llama import (
    QEffLlamaDecoderLayer,
    QEffLlamaRotaryEmbedding,
    qeff_apply_rotary_pos_emb,
)


class LlamaSwiftKVAttention(nn.Module):
    def __init__(self, config, layer_idx) -> None:
        super().__init__()
        self.hidden_size = config.hidden_size
        self.attention_dropout = config.attention_dropout
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = getattr(config, "head_dim", self.hidden_size // self.num_heads)
        self.num_key_value_heads = config.num_key_value_heads
        self.num_key_value_groups = self.num_heads // self.num_key_value_heads
        self.max_position_embeddings = config.max_position_embeddings
        self.rope_theta = config.rope_theta
        self.is_causal = True
        self.layer_idx = layer_idx
        self.q_proj_swiftkv = nn.Linear(self.hidden_size, self.num_heads * self.head_dim, bias=config.attention_bias)
        self.k_proj_swiftkv = nn.Linear(
            self.hidden_size, self.num_key_value_heads * self.head_dim, bias=config.attention_bias
        )
        self.v_proj_swiftkv = nn.Linear(
            self.hidden_size, self.num_key_value_heads * self.head_dim, bias=config.attention_bias
        )
        self.o_proj = nn.Linear(self.num_heads * self.head_dim, self.hidden_size, bias=config.attention_bias)

        self.rotary_emb = QEffLlamaRotaryEmbedding(config=config)

    def forward(
        self,
        hidden_states: torch.Tensor,
        position_ids,
        past_key_value: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        attention_mask=None,
    ) -> torch.Tensor:
        bsz, q_len, _ = hidden_states.size()
        query, _ = self.q_proj_swiftkv(hidden_states)

        # Reshape the query, key, and value tensors.
        query_states = query.view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)

        kv_seq_len = position_ids.shape[-1]
        if past_key_value is not None:
            if self.layer_idx is None:
                raise ValueError(
                    f"The cache structure has changed since version v4.36. If you are using {self.__class__.__name__} "
                    "for auto-regressive decoding with k/v caching, please make sure to initialize the attention class "
                    "with a layer index."
                )
            kv_seq_len = past_key_value.get_usable_length(kv_seq_len, self.layer_idx)

        key_states, value_states = past_key_value.read_only(self.layer_idx, position_ids=position_ids)
        cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
        query_states, _ = qeff_apply_rotary_pos_emb(query_states, torch.empty_like(key_states), cos, sin, position_ids)

        key_states = repeat_kv(key_states, self.num_key_value_groups)
        value_states = repeat_kv(value_states, self.num_key_value_groups)

        attn_weights = torch.matmul(query_states, key_states.transpose(2, 3)) / math.sqrt(self.head_dim)

        if attention_mask is not None:  # no matter the length, we just slice it
            attn_weights = torch.where(attention_mask, torch.tensor(-10000.0, dtype=torch.float32), attn_weights)

        # upcast attention to fp32
        attn_weights = nn.functional.softmax(attn_weights, dim=-1, dtype=torch.float32).to(query_states.dtype)
        # attn_weights = nn.functional.dropout(attn_weights, p=self.attention_dropout, training=self.training)
        attn_output = torch.matmul(attn_weights, value_states)

        if attn_output.size() != (bsz, self.num_heads, q_len, self.head_dim):
            raise ValueError(
                f"`attn_output` should be of size {(bsz, self.num_heads, q_len, self.head_dim)}, but is"
                f" {attn_output.size()}"
            )

        attn_output = attn_output.transpose(1, 2).contiguous()

        attn_output = attn_output.reshape(bsz, q_len, self.hidden_size)
        attn_output = self.o_proj(attn_output)

        return attn_output, past_key_value


class LlamaSwiftKVDecoderLayer(nn.Module):
    def __init__(self, config, layer_idx) -> None:
        super().__init__()
        self.hidden_size = config.hidden_size
        self.num_key_value_heads = config.num_key_value_heads

        self.self_attn = LlamaSwiftKVAttention(config=config, layer_idx=layer_idx)
        self.mlp = LlamaMLP(config)
        self.input_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(
        self, hidden_states: torch.Tensor, position_ids: torch.Tensor, past_key_values, causal_mask
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # Self Attention
        residual = hidden_states
        hidden_states = self.input_layernorm(hidden_states)

        hidden_states, past_key_values = self.self_attn(
            hidden_states=hidden_states,
            position_ids=position_ids,
            past_key_value=past_key_values,
            attention_mask=causal_mask,
        )

        hidden_states = residual + hidden_states
        # Fully Connected
        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + hidden_states

        return hidden_states, past_key_values


class LlamaSwiftKVModel(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.vocab_size = config.vocab_size
        self.config = config

        self.embed_tokens = nn.Embedding(
            self.vocab_size, config.hidden_size, None
        )  # TODO: Not sure if padding_idx shoudl eb NONE
        self.layers = torch.nn.ModuleList(
            [
                QEffLlamaDecoderLayer(config=config, layer_idx=idx)
                if idx < config.num_key_value_layers
                else LlamaSwiftKVDecoderLayer(config=config, layer_idx=idx)
                for idx in range(config.num_hidden_layers)
            ]
        )
        self.norm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.norm_swiftkv = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def _run_swiftkv_layers(
        self, hidden_states: torch.Tensor, position_ids: torch.Tensor, past_key_values, causal_mask
    ) -> torch.Tensor:
        for layer_idx in range(self.config.num_key_value_layers, self.config.num_hidden_layers):
            layer = self.layers[layer_idx]

            hidden_states, past_key_values = layer(hidden_states, position_ids, past_key_values, causal_mask)

        return hidden_states, past_key_values

    def _update_causal_mask(
        self,
        attention_mask: torch.Tensor,
        input_tensor: torch.Tensor,
        cache_position: torch.Tensor,
        position_ids: torch.Tensor,
        past_key_values: Cache,
        output_attentions: bool,
    ):
        self.config._attn_implementation = "eager"
        if self.config._attn_implementation == "flash_attention_2":
            if attention_mask is not None and 0.0 in attention_mask:
                return attention_mask
            return None

        # For SDPA, when possible, we will rely on its `is_causal` argument instead of its `attn_mask` argument, in
        # order to dispatch on Flash Attention 2. This feature is not compatible with static cache, as SDPA will fail
        # to infer the attention mask.
        past_seen_tokens = past_key_values.get_seq_length() if past_key_values is not None else 0
        using_static_cache = isinstance(past_key_values, StaticCache)

        # When output attentions is True, sdpa implementation's forward method calls the eager implementation's forward
        if self.config._attn_implementation == "sdpa" and not using_static_cache and not output_attentions:
            if AttentionMaskConverter._ignore_causal_mask_sdpa(
                attention_mask,
                inputs_embeds=input_tensor,
                past_key_values_length=past_seen_tokens,
                is_training=self.training,
            ):
                return None

        dtype, device = input_tensor.dtype, input_tensor.device
        min_dtype = torch.finfo(dtype).min
        sequence_length = input_tensor.shape[1]
        if using_static_cache:
            target_length = past_key_values.get_max_length()
        else:
            target_length = attention_mask.shape[-1] if isinstance(attention_mask, torch.Tensor) else past_seen_tokens

        if attention_mask is not None and attention_mask.dim() == 4:
            # in this case we assume that the mask comes already in inverted form and requires no inversion or slicing
            if attention_mask.max() != 0:
                raise ValueError("Custom 4D attention mask should be passed in inverted form with max==0`")
            causal_mask = attention_mask
        else:
            causal_mask = torch.full((sequence_length, target_length), fill_value=min_dtype, dtype=dtype, device=device)
            if sequence_length != 1:
                causal_mask = torch.triu(causal_mask, diagonal=1)
            causal_mask *= torch.arange(target_length, device=device) > cache_position.reshape(-1, 1)
            causal_mask = causal_mask[None, None, :, :].expand(input_tensor.shape[0], 1, -1, -1)
            if attention_mask is not None:
                causal_mask = causal_mask.clone()  # copy to contiguous memory for in-place edit
                mask_length = attention_mask.shape[-1]
                padding_mask = causal_mask[:, :, :, :mask_length] + attention_mask[:, None, None, :]
                padding_mask = padding_mask == 0
                causal_mask[:, :, :, :mask_length] = causal_mask[:, :, :, :mask_length].masked_fill(
                    padding_mask, min_dtype
                )
            else:
                causal_mask = _create_causal_mask(position_ids=position_ids, target_length=target_length)

        if (
            self.config._attn_implementation == "sdpa"
            and attention_mask is not None
            and attention_mask.device.type == "cuda"
            and not output_attentions
        ):
            # Attend to all tokens in fully masked rows in the causal_mask, for example the relevant first rows when
            # using left padding. This is required by F.scaled_dot_product_attention memory-efficient attention path.
            # Details: https://github.com/pytorch/pytorch/issues/110213
            causal_mask = AttentionMaskConverter._unmask_unattended(causal_mask, min_dtype)

        return causal_mask

    def forward(
        self,
        input_ids: Optional[torch.Tensor],
        position_ids: torch.Tensor,
        past_key_values: List[torch.Tensor],
    ):
        inputs_embeds = self.embed_tokens(input_ids)

        # kept for BC (non `Cache` `past_key_values` inputs)
        use_cache = True

        if use_cache and not isinstance(past_key_values, Cache):
            if past_key_values is None:
                past_key_values = QEffDynamicCache()
            else:
                past_key_values = QEffDynamicCache.from_legacy_cache(past_key_values)
                logger.warning_once(
                    "We detected that you are passing `past_key_values` as a tuple of tuples. This is deprecated and "
                    "will be removed in v4.47. Please convert your cache or use an appropriate `Cache` class "
                    "(https://huggingface.co/docs/transformers/kv_cache#legacy-cache-format)"
                )

        past_seen_tokens = past_key_values.get_seq_length() if past_key_values is not None else 0
        cache_position = torch.arange(
            past_seen_tokens, past_seen_tokens + inputs_embeds.shape[1], device=inputs_embeds.device
        )
        if position_ids is None:
            position_ids = cache_position.unsqueeze(0)

        causal_mask = self._update_causal_mask(
            None, inputs_embeds, cache_position, position_ids, past_key_values, False
        )
        hidden_states = inputs_embeds

        # create position embeddings to be shared across the decoder layers
        # position_embeddings = self.rotary_emb(hidden_states, position_ids)
        next_decoder_cache = None

        for layer_idx in range(self.config.num_key_value_layers):
            layer = self.layers[layer_idx]
            hidden_states, next_decoder_cache = layer(
                hidden_states,
                attention_mask=causal_mask,
                position_ids=position_ids,
                past_key_value=past_key_values,
                output_attentions=False,
                use_cache=True,
                cache_position=cache_position,
                position_embeddings=None,
            )

        bsz, q_len, _ = hidden_states.size()
        swiftkv_hidden_states = self.norm_swiftkv(hidden_states)

        ####################################
        ## THE MAGIC OF SWIFT KV BEGINS HERE
        ####################################
        for layer_idx in range(self.config.num_key_value_layers, self.config.num_hidden_layers):
            self_attn = self.layers[layer_idx].self_attn
            key_states = self_attn.k_proj_swiftkv(swiftkv_hidden_states)
            value_states = self_attn.v_proj_swiftkv(swiftkv_hidden_states)
            key_states = key_states.view(bsz, q_len, self_attn.num_key_value_heads, self_attn.head_dim).transpose(1, 2)
            value_states = value_states.view(bsz, q_len, self_attn.num_key_value_heads, self_attn.head_dim).transpose(
                1, 2
            )

            kv_seq_len = key_states.shape[-2]
            if past_key_values is not None:
                if self_attn.layer_idx is None:
                    raise ValueError(
                        f"The cache structure has changed since version v4.36. If you are using {self_attn.__class__.__name__} "
                        "for auto-regressive decoding with k/v caching, please make sure to initialize the attention class "
                        "with a layer index."
                    )
                kv_seq_len = past_key_values.get_usable_length(kv_seq_len, self_attn.layer_idx)

            cos, sin = self_attn.rotary_emb(value_states, seq_len=kv_seq_len)
            _, key_states = qeff_apply_rotary_pos_emb(
                torch.empty_like(swiftkv_hidden_states), key_states, cos, sin, position_ids
            )
            cache_kwargs = {"sin": sin, "cos": cos, "position_ids": position_ids}
            past_key_values.write_only(key_states, value_states, self_attn.layer_idx, cache_kwargs)

        hidden_states, next_decoder_cache = self._run_swiftkv_layers(
            hidden_states, position_ids, past_key_values, causal_mask
        )
        ####################################
        ## THE MAGIC OF SWIFT KV ENDS HERE
        ####################################

        next_cache = next_decoder_cache.to_legacy_cache()
        return hidden_states, next_cache


class LlamaSwiftKVForCausalLM(nn.Module):
    """
    # packed_modules_mapping = {
    #     "kv_proj_swiftkv": ["k_proj_swiftkv", "v_proj_swiftkv"],
    #     "qkv_proj": ["q_proj", "k_proj", "v_proj"],
    #     "gate_up_proj": ["gate_proj", "up_proj"],
    # }

    # # BitandBytes specific attributes
    # default_bitsandbytes_target_modules = [
    #     ".gate_proj.",
    #     ".down_proj.",
    #     ".up_proj.",
    #     ".q_proj.",
    #     ".k_proj.",
    #     ".v_proj.",
    #     ".o_proj.",
    #     ".k_proj_swiftkv.",
    #     ".v_proj_swiftkv.",
    # ]

    # # in TP, these weights are partitioned along the column dimension (dim=-1)
    # column_parallel_weights_modules = [
    #     ".q_proj_swiftkv.",
    #     ".down_proj.",
    #     ".o_proj.",
    # ]
    # bitsandbytes_stacked_params_mapping = {
    #     # shard_name, weight_name, index
    #     "k_proj_swiftkv": ("kv_proj_swiftkv", 1),
    #     "v_proj_swiftkv": ("kv_proj_swiftkv", 2),
    #     "q_proj": ("qkv_proj", 0),
    #     "k_proj": ("qkv_proj", 1),
    #     "v_proj": ("qkv_proj", 2),
    #     "gate_proj": ("gate_up_proj", 0),
    #     "up_proj": ("gate_up_proj", 1),
    # }
    """

    def __init__(self, *, config):
        super().__init__()

        self.model = LlamaSwiftKVModel(
            config=config,
        )
        self.vocab_size = config.vocab_size
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)
        self.config = config

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        past_key_values: Optional[Union[List[torch.FloatTensor]]] = None,
    ):
        hidden_states, output_past_key_values = self.model(input_ids, position_ids, past_key_values)
        logit_index = position_ids.to(torch.int32).argmax(1, keepdim=True)
        hidden_states = hidden_states[torch.arange(position_ids.shape[0]).view(-1, 1), logit_index]
        logits = self.lm_head(hidden_states)
        return logits, output_past_key_values
