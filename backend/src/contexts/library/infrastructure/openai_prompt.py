import html

from src.contexts.library.domain.chat import ChatContext, ChatRole


SYSTEM_PROMPT = """You are a reading companion for one book. The reader is partway through it and
has asked about the section they are on.

Answer from the book excerpt below. It is a window around where the reader
currently is -- a few thousand words, not the whole book. When the excerpt does
not contain the answer, say so plainly and say what it does cover; if you add
anything from outside the excerpt, label it as outside the book.

Do not reveal or summarise anything from later in the book than the excerpt.

Keep answers to the length the question needs, usually two or three sentences.
Quote the book only when the exact wording is the point.

<book title="{title}" sections="{sections}" of="{of}">
{sections_xml}
</book>"""


def render_messages(context: ChatContext) -> list[dict]:
    """PLANS/phase-7.md §6.3. Pure function to build the OpenAI messages array."""
    title_esc = html.escape(context.book_title, quote=True)
    
    start_section = context.chunks[0].index if context.chunks else 0
    end_section = context.chunks[-1].index if context.chunks else 0
    sections_str = f"{start_section}-{end_section}" if start_section != end_section else str(start_section)
    
    sections_xml_parts = []
    for chunk in context.chunks:
        text_esc = html.escape(chunk.text, quote=False)
        pages_attr = f' pages="{chunk.page_start}-{chunk.page_end}"' if chunk.page_start and chunk.page_end else ""
        reading_here = ' reading-here="true"' if chunk.is_anchor else ""
        sections_xml_parts.append(
            f'<section index="{chunk.index}"{pages_attr}{reading_here}>{text_esc}</section>'
        )
    
    system_content = SYSTEM_PROMPT.format(
        title=title_esc,
        sections=sections_str,
        of=context.chunks_total,
        sections_xml="\n".join(sections_xml_parts)
    )
    
    messages = [{"role": "system", "content": system_content}]
    
    for msg in context.history:
        messages.append({"role": msg.role.value, "content": msg.content})
        
    messages.append({"role": "user", "content": context.question})
    
    return messages
