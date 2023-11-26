import time
from typing import Dict, List, Optional, Set, Tuple

import torch
import torch.nn as nn

from vllm.config import ModelConfig, ParallelConfig, SchedulerConfig
from vllm.logger import init_logger
from vllm.model_executor import get_model, InputMetadata, SamplingMetadata
from vllm.sampling_params import SamplingParams, SamplingType
from vllm.sequence import SamplerOutput, SequenceData, SequenceGroupMetadata

logger = init_logger(__name__)

BATCH_SIZES_TO_COMPILE = [1, 2, 4, 8] + [8 * i for i in range(2, 17)]


class ModelRunner:

    def __init__(
        self,
        model_config: ModelConfig,
        parallel_config: ParallelConfig,
        scheduler_config: SchedulerConfig,
    ):
        self.model_config = model_config
        self.parallel_config = parallel_config
        self.scheduler_config = scheduler_config

        self.sliding_window = model_config.get_sliding_window()
        self.model = None
        self.compiled_model = None
        self.compiled_batch_sizes: Set[int] = set()
        self.block_size = None

    def load_model(self) -> None:
        model = get_model(self.model_config)
        num_layers = self.model_config.get_num_layers(self.parallel_config)
        self.model = ModelWrapper(model, kv_caches=[(None, None)] * num_layers)

    def set_kv_cache(
        self,
        kv_caches: List[Tuple[torch.Tensor, torch.Tensor]],
        block_size: int,
    ) -> None:
        self.model.kv_caches = kv_caches
        self.block_size = block_size

    def _prepare_prompt(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
    ) -> Tuple[torch.Tensor, torch.Tensor, InputMetadata]:
        assert len(seq_group_metadata_list) > 0
        input_tokens: List[List[int]] = []
        input_positions: List[List[int]] = []
        slot_mapping: List[List[int]] = []

        prompt_lens: List[int] = []
        for seq_group_metadata in seq_group_metadata_list:
            assert seq_group_metadata.is_prompt
            seq_ids = list(seq_group_metadata.seq_data.keys())
            assert len(seq_ids) == 1
            seq_id = seq_ids[0]

            seq_data = seq_group_metadata.seq_data[seq_id]
            prompt_tokens = seq_data.get_token_ids()
            prompt_len = len(prompt_tokens)
            prompt_lens.append(prompt_len)

            input_tokens.append(prompt_tokens)
            # NOTE(woosuk): Here we assume that the first token in the prompt
            # is always the first token in the sequence.
            input_positions.append(list(range(prompt_len)))

            if seq_group_metadata.block_tables is None:
                # During memory profiling, the block tables are not initialized
                # yet. In this case, we just use a dummy slot mapping.
                slot_mapping.append([-1] * prompt_len)
                continue

            # Compute the slot mapping.
            # FIXME: Handle sliding window here.
            slot_mapping.append([])
            block_table = seq_group_metadata.block_tables[seq_id]
            for i in range(prompt_len):
                block_number = block_table[i // self.block_size]
                block_offset = i % self.block_size
                slot = block_number * self.block_size + block_offset
                slot_mapping[-1].append(slot)

        max_prompt_len = max(prompt_lens)
        input_tokens = _make_tensor_with_pad(input_tokens,
                                             max_prompt_len,
                                             pad=0,
                                             dtype=torch.long)
        input_positions = _make_tensor_with_pad(input_positions,
                                                max_prompt_len,
                                                pad=0,
                                                dtype=torch.long)
        slot_mapping = _make_tensor_with_pad(slot_mapping,
                                             max_prompt_len,
                                             pad=-1,
                                             dtype=torch.long)

        input_metadata = InputMetadata(
            prompt_lens=prompt_lens,
            slot_mapping=slot_mapping,
            context_lens=None,
            block_tables=None,
        )
        return input_tokens, input_positions, input_metadata

    def _prepare_decode(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
        padded_batch_size: Optional[int],
    ) -> Tuple[torch.Tensor, torch.Tensor, InputMetadata]:
        assert len(seq_group_metadata_list) > 0
        input_tokens: List[List[int]] = []
        input_positions: List[List[int]] = []
        slot_mapping: List[List[int]] = []
        context_lens: List[int] = []
        block_tables: List[List[int]] = []

        for seq_group_metadata in seq_group_metadata_list:
            assert not seq_group_metadata.is_prompt

            seq_ids = list(seq_group_metadata.seq_data.keys())
            for seq_id in seq_ids:
                seq_data = seq_group_metadata.seq_data[seq_id]
                generation_token = seq_data.get_last_token_id()
                input_tokens.append([generation_token])

                context_len = seq_data.get_len()
                if self.sliding_window is not None:
                    context_len = min(context_len, self.sliding_window)
                context_lens.append(context_len)

                position = context_len - 1
                input_positions.append([position])

                block_table = seq_group_metadata.block_tables[seq_id]
                block_number = block_table[position // self.block_size]
                block_offset = position % self.block_size
                slot = block_number * self.block_size + block_offset
                slot_mapping.append([slot])

                # FIXME: Handle sliding window here.
                block_tables.append(block_table)

        batch_size = len(input_tokens)
        if padded_batch_size is not None:
            assert batch_size <= padded_batch_size
            for _ in range(padded_batch_size - batch_size):
                input_tokens.append([])
                input_positions.append([])
                slot_mapping.append([])
                context_lens.append(1)
                block_tables.append([])

        input_tokens = _make_tensor_with_pad(input_tokens,
                                             max_len=1,
                                             pad=0,
                                             dtype=torch.long)
        input_positions = _make_tensor_with_pad(input_positions,
                                                max_len=1,
                                                pad=0,
                                                dtype=torch.long)
        slot_mapping = _make_tensor_with_pad(slot_mapping,
                                             max_len=1,
                                             pad=-1,
                                             dtype=torch.long)
        context_lens = torch.tensor(context_lens,
                                    dtype=torch.int,
                                    device="cuda")
        block_tables = _make_tensor_with_pad(
            block_tables,
            max_len=1000,  # FIXME
            pad=0,
            dtype=torch.int)

        input_metadata = InputMetadata(
            prompt_lens=[],
            slot_mapping=slot_mapping,
            context_lens=context_lens,
            block_tables=block_tables,
        )
        return input_tokens, input_positions, input_metadata

    def _prepare_sample(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
        prompt_lens: List[int],
    ) -> SamplingMetadata:
        seq_groups: List[Tuple[List[int], SamplingParams]] = []
        selected_token_indices: List[int] = []
        selected_token_start_idx = 0
        categorized_sample_indices = {t: [] for t in SamplingType}
        categorized_sample_indices_start_idx = 0

        max_prompt_len = max(prompt_lens) if prompt_lens else 1
        for i, seq_group_metadata in enumerate(seq_group_metadata_list):
            seq_ids = list(seq_group_metadata.seq_data.keys())
            sampling_params = seq_group_metadata.sampling_params
            seq_groups.append((seq_ids, sampling_params))

            if seq_group_metadata.is_prompt:
                assert len(seq_ids) == 1
                prompt_len = prompt_lens[i]
                if sampling_params.prompt_logprobs is not None:
                    # NOTE: prompt token positions do not need sample, skip
                    categorized_sample_indices_start_idx += prompt_len - 1

                categorized_sample_indices[
                    sampling_params.sampling_type].append(
                        categorized_sample_indices_start_idx)
                categorized_sample_indices_start_idx += 1

                if sampling_params.prompt_logprobs is not None:
                    selected_token_indices.extend(
                        range(selected_token_start_idx,
                              selected_token_start_idx + prompt_len - 1))
                selected_token_indices.append(selected_token_start_idx +
                                              prompt_len - 1)
                selected_token_start_idx += max_prompt_len
            else:
                num_seqs = len(seq_ids)
                selected_token_indices.extend(
                    range(selected_token_start_idx,
                          selected_token_start_idx + num_seqs))
                selected_token_start_idx += num_seqs

                categorized_sample_indices[
                    sampling_params.sampling_type].extend(
                        range(categorized_sample_indices_start_idx,
                              categorized_sample_indices_start_idx + num_seqs))
                categorized_sample_indices_start_idx += num_seqs

        selected_token_indices = torch.tensor(selected_token_indices,
                                              dtype=torch.long,
                                              device="cuda")
        categorized_sample_indices = {
            t: torch.tensor(seq_ids, dtype=torch.int, device="cuda")
            for t, seq_ids in categorized_sample_indices.items()
        }

        seq_data: Dict[int, SequenceData] = {}
        for seq_group_metadata in seq_group_metadata_list:
            seq_data.update(seq_group_metadata.seq_data)

        sampling_metadata = SamplingMetadata(
            seq_groups=seq_groups,
            seq_data=seq_data,
            prompt_lens=prompt_lens,
            selected_token_indices=selected_token_indices,
            categorized_sample_indices=categorized_sample_indices,
        )
        return sampling_metadata

    @torch.inference_mode()
    def execute_model(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
    ) -> SamplerOutput:
        # NOTE: We assume that all sequences in the group are all prompts or
        # all decodes.
        is_prompt = seq_group_metadata_list[0].is_prompt
        batch_size = sum(
            len(metadata.seq_data) for metadata in seq_group_metadata_list)
        padded_batch_size = None
        if not self.model_config.enforce_eager and not is_prompt:
            padded_batch_size = _get_padded_batch_size(batch_size)

        # Prepare input tensors.
        if is_prompt:
            inputs = self._prepare_prompt(seq_group_metadata_list)
            input_tokens, input_positions, input_metadata = inputs
        else:
            inputs = self._prepare_decode(seq_group_metadata_list,
                                          padded_batch_size)
            input_tokens, input_positions, input_metadata = inputs
        sampling_metadata = self._prepare_sample(seq_group_metadata_list,
                                                 input_metadata.prompt_lens)

        # Execute the model.
        use_compiled_model = padded_batch_size is not None
        model_executable = (self.compiled_model
                            if use_compiled_model else self.model)
        hidden_states = model_executable(
            input_ids=input_tokens,
            positions=input_positions,
            input_metadata=input_metadata,
        )
        if batch_size != padded_batch_size:
            hidden_states = hidden_states[:batch_size]

        # Sample the next token.
        output = self.model.sample(
            hidden_states=hidden_states,
            sampling_metadata=sampling_metadata,
        )
        return output

    @torch.inference_mode()
    def profile_run(self) -> None:
        # Enable top-k sampling to reflect the accurate memory usage.
        vocab_size = self.model_config.get_vocab_size()
        sampling_params = SamplingParams(top_p=0.99, top_k=vocab_size - 1)
        max_num_batched_tokens = self.scheduler_config.max_num_batched_tokens
        max_num_seqs = self.scheduler_config.max_num_seqs

        # Profile memory usage with max_num_sequences sequences and the total
        # number of tokens equal to max_num_batched_tokens.
        seqs: List[SequenceGroupMetadata] = []
        for group_id in range(max_num_seqs):
            seq_len = (max_num_batched_tokens // max_num_seqs +
                       (group_id < max_num_batched_tokens % max_num_seqs))
            seq_data = SequenceData([0] * seq_len)
            seq = SequenceGroupMetadata(
                request_id=str(group_id),
                is_prompt=True,
                seq_data={group_id: seq_data},
                sampling_params=sampling_params,
                block_tables=None,
            )
            seqs.append(seq)

        # Run the model with the dummy inputs.
        self.execute_model(seqs)
        return

    @torch.inference_mode()
    def compile_model(self) -> None:
        assert not self.model_config.enforce_eager
        logger.info("Compiling the model with torch.compile. This may take "
                    "several minutes. If you want to avoid the compilation "
                    "at the cost of inference speed, you can set "
                    "enforce_eager=True or --enforce-eager in CLI.")

        start_time = time.perf_counter()
        self.compiled_model = torch.compile(self.model,
                                            mode="reduce-overhead",
                                            fullgraph=True)

        for batch_size in reversed(BATCH_SIZES_TO_COMPILE):
            # Create dummy inputs.
            input_tokens = _make_tensor_with_pad([[]] * batch_size,
                                                 max_len=1,
                                                 pad=0,
                                                 dtype=torch.long)
            input_positions = _make_tensor_with_pad([[]] * batch_size,
                                                    max_len=1,
                                                    pad=0,
                                                    dtype=torch.long)
            slot_mapping = _make_tensor_with_pad([[]] * batch_size,
                                                 max_len=1,
                                                 pad=-1,
                                                 dtype=torch.long)
            context_lens = torch.tensor([1] * batch_size,
                                        dtype=torch.int,
                                        device="cuda")
            block_tables = _make_tensor_with_pad(
                [[]] * batch_size,
                max_len=1000,  # FIXME
                pad=0,
                dtype=torch.int)
            input_metadata = InputMetadata(
                prompt_lens=[],
                slot_mapping=slot_mapping,
                context_lens=context_lens,
                block_tables=block_tables,
            )

            # Run the model with the dummy inputs.
            self.compiled_model(
                input_tokens,
                input_positions,
                input_metadata,
            )

        end_time = time.perf_counter()
        compile_time = end_time - start_time
        logger.info(f"Model compilation finished in {compile_time:.0f} s.")


class ModelWrapper(nn.Module):

    def __init__(
        self,
        model: nn.Module,
        kv_caches: List[Tuple[torch.Tensor, torch.Tensor]],
    ):
        super().__init__()
        self.model = model
        self.kv_caches = kv_caches

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        input_metadata: InputMetadata,
    ) -> torch.Tensor:
        hidden_states = self.model(
            input_ids,
            positions,
            self.kv_caches,
            input_metadata,
        )
        return hidden_states

    def sample(
        self,
        hidden_states: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> SamplerOutput:
        return self.model.sample(hidden_states, sampling_metadata)


def _pad_to_max(x: List[int], max_len: int, pad: int) -> List[int]:
    assert len(x) <= max_len
    return x + [pad] * (max_len - len(x))


def _make_tensor_with_pad(
    x: List[List[int]],
    max_len: int,
    pad: int,
    dtype: torch.dtype,
) -> torch.Tensor:
    padded_x = [_pad_to_max(x_i, max_len, pad) for x_i in x]
    return torch.tensor(padded_x, dtype=dtype, device="cuda")


def _get_padded_batch_size(batch_size: int) -> Optional[int]:
    if batch_size <= 2:
        return batch_size
    elif batch_size <= 4:
        return 4
    elif batch_size <= BATCH_SIZES_TO_COMPILE[-1]:
        return (batch_size + 7) // 8 * 8
    else:
        return None
