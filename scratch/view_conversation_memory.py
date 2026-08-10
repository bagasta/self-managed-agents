import sys
sys.path.insert(0, ".")
import asyncio
import sqlalchemy as sa

import app.database as db
from app.models.conversation_summary import ConversationSummary
from app.core.domain.markdown_generator import generate_summary_markdown

async def main():
    async with db.AsyncSessionLocal() as session:
        result = await session.execute(
            sa.select(ConversationSummary).order_by(ConversationSummary.created_at.desc())
        )
        summaries = result.scalars().all()
        
        print(f"=== Total Summaries Stored in DB: {len(summaries)} ===\n")
        
        if not summaries:
            print("No summaries found in conversation_summaries table yet.")
            print("Summaries are automatically created when session turn counts exceed threshold.")
            return

        for idx, s in enumerate(summaries, 1):
            print(f"--- Summary #{idx} ---")
            print(f"ID              : {s.id}")
            print(f"Session ID      : {s.session_id}")
            print(f"Is Active       : {s.is_active}")
            print(f"Msg Count At    : {s.message_count_at}")
            print(f"Token Estimate  : {s.token_estimate}")
            print(f"Created At      : {s.created_at}")
            print("\n[Raw Text Stored in DB Column 'summary_text']:")
            print(s.summary_text)
            print("\n[Formatted Markdown Context (Generated in Memory for LLM Prompt)]:")
            print(generate_summary_markdown(s.summary_text))
            print("=" * 60 + "\n")

if __name__ == "__main__":
    asyncio.run(main())
