"""
Chapter output normalizer.

Chapter detection is now performed by the multimodal LLM during transcription
(see :func:`~backend.services.openrouter_client.transcribe_audio`).

This module provides a single helper that converts the LLM's chapters list into
the normalized dict format expected by the processor.
"""


def parse_llm_chapters(llm_chapters: list[dict]) -> list[dict]:
    """Convert LLM chapter output to normalized chapter format.

    Parameters
    ----------
    llm_chapters:
        The ``chapters`` list returned by the transcription LLM, where each
        element is expected to contain ``chapter_number``, ``title``, and
        ``transcription`` keys.

    Returns
    -------
    list of dict
        Each dict has keys ``chapter_number`` (int), ``title`` (str), and
        ``transcription`` (str).  Missing fields are filled with safe defaults.
    """
    result = []
    for ch in llm_chapters:
        chapter_number = ch.get("chapter_number", 1)
        result.append({
            "chapter_number": chapter_number,
            "title": ch.get("title", f"Chapter {chapter_number}"),
            "transcription": ch.get("transcription", ""),
        })
    return result
