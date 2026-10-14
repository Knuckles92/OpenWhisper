"""Several Qwen windows per generate call, as many as the GPU has room for.

Runs in the speech worker on the Qwen runtime's PyTorch. One window per call
left an RTX 5080 about two-thirds idle (#48); batching shares each decoding
step's fixed cost between windows.
"""
from __future__ import annotations

#: Windows per worker request. A 5080 decoding 1.7B kept getting faster up
#: to 16 per call; smaller cards stop at what ``memory_batch_size`` allows.
MAX_BATCH_SIZE = 16
_MIB = 1024 * 1024
#: Left free for the desktop and other apps.
_RESERVE = 512 * _MIB
#: Peak reserved memory per window beyond the first, measured on 30 s windows.
_PER_WINDOW = 256 * _MIB


def memory_batch_size(torch, limit: int) -> int:
    """How many windows fit in the GPU's free memory now, from 1 to ``limit``.

    Windows drivers page an overcommitted GPU out to system memory instead of
    raising out-of-memory, which makes decoding far slower but never fails,
    so this has to be right without an error to correct it.
    """
    free, _total = torch.cuda.mem_get_info()
    # Memory this process reserved but isn't using is free to it too.
    unused = torch.cuda.memory_reserved() - torch.cuda.memory_allocated()
    return max(1, min(limit, 1 + (free + unused - _RESERVE) // _PER_WINDOW))


def transcribe_batch(engine, audios, language, torch, limit):
    """The texts of ``audios`` in order, and the limit for the file's next request.

    Running out of GPU memory halves the limit and retries. Any other failure
    of a call with several windows decodes them one at a time, as without
    batching: one window reaching the output limit fails its whole call, and
    alone it raises the error the user would have seen anyway.
    """
    texts = []
    try:
        while len(texts) < len(audios):
            group = audios[len(texts):len(texts) + memory_batch_size(torch, limit)]
            try:
                results = engine.transcribe(audio=[(audio, 16000) for audio in group], language=language)
            except Exception as exc:
                if len(group) == 1:
                    raise
                out_of_memory = isinstance(exc, torch.cuda.OutOfMemoryError)
            else:
                texts.extend(result.text for result in results)
                continue
            # Retried out here, where the failed call's tensors were released
            # along with its exception.
            if out_of_memory:
                limit = max(1, len(group) // 2)
            else:
                texts.extend(engine.transcribe(audio=(audio, 16000), language=language)[0].text
                             for audio in group)
        return texts, limit
    finally:
        # Return the batch's working memory, so a long upload doesn't keep
        # its peak reserved for as long as the engine stays loaded.
        torch.cuda.empty_cache()
