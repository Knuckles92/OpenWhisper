"""
Insights Service for generating meeting insights from transcriptions.
Uses LLM providers (OpenAI/OpenRouter) to analyze meeting transcripts.
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Callable, Dict, Any, List
from enum import Enum

from services.llm_provider import LLMProvider, get_llm_provider
from services.settings import settings_manager
from services.database import db


class InsightType(Enum):
    """Types of insights that can be generated."""
    SUMMARY = "summary"
    ACTION_ITEMS = "action_items"
    CUSTOM = "custom"


@dataclass
class InsightEntry:
    """Represents a saved insight entry."""
    id: int
    meeting_id: str
    insight_type: str
    content: str
    custom_prompt: Optional[str]
    generated_at: str
    provider: Optional[str]
    model: Optional[str]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'InsightEntry':
        """Create an InsightEntry from a dictionary."""
        return cls(
            id=data['id'],
            meeting_id=data['meeting_id'],
            insight_type=data['insight_type'],
            content=data['content'],
            custom_prompt=data.get('custom_prompt'),
            generated_at=data['generated_at'],
            provider=data.get('provider'),
            model=data.get('model')
        )

    @property
    def generated_at_datetime(self) -> datetime:
        """Parse generated_at as datetime."""
        return datetime.fromisoformat(self.generated_at)

    @property
    def type_enum(self) -> InsightType:
        """Get the InsightType enum value."""
        return InsightType(self.insight_type)


# Prompt templates for different insight types
PROMPT_TEMPLATES = {
    InsightType.SUMMARY: {
        "system": """You are an expert meeting analyst. Your task is to analyze meeting transcripts and provide clear, concise summaries that capture the essential information discussed.

Focus on:
- Main topics and themes discussed
- Key decisions made
- Important points raised by participants
- Any conclusions or outcomes

Format your summary with clear sections and bullet points for readability.""",
        
        "user": """Please analyze the following meeting transcript and provide a comprehensive summary:

---
TRANSCRIPT:
{transcript}
---

Provide a well-structured summary that includes:
1. **Overview**: A brief 2-3 sentence summary of the meeting
2. **Key Topics Discussed**: Main subjects covered
3. **Decisions Made**: Any decisions or agreements reached
4. **Notable Points**: Important insights or information shared
5. **Next Steps**: Any mentioned follow-up actions (if applicable)"""
    },
    
    InsightType.ACTION_ITEMS: {
        "system": """You are an expert at extracting action items and tasks from meeting transcripts. Your goal is to identify all commitments, tasks, and follow-up items mentioned during the meeting.

For each action item, try to identify:
- What needs to be done
- Who is responsible (if mentioned)
- Any deadlines or timeframes (if mentioned)
- Priority level (if discernible)

Be thorough but avoid creating action items that weren't actually discussed.""",
        
        "user": """Please extract all action items and tasks from the following meeting transcript:

---
TRANSCRIPT:
{transcript}
---

Format your response as a clear list of action items. For each item include:
- **Task**: Description of what needs to be done
- **Owner**: Who is responsible (if mentioned, otherwise "Unassigned")
- **Deadline**: When it's due (if mentioned, otherwise "Not specified")
- **Priority**: High/Medium/Low (based on context)

If no clear action items were discussed, indicate that the meeting was primarily informational."""
    },
    
    InsightType.CUSTOM: {
        "system": """You are an expert meeting analyst who can provide various types of analysis on meeting transcripts based on specific requests. Provide clear, actionable insights based on the user's specific question or request.""",
        
        "user": """Analyze the following meeting transcript based on the specific request below:

---
TRANSCRIPT:
{transcript}
---

SPECIFIC REQUEST:
{custom_prompt}

Provide a clear and helpful response based on the transcript content."""
    }
}


class InsightsService:
    """Service for generating insights from meeting transcriptions."""
    
    def __init__(self):
        """Initialize the insights service."""
        self._provider: Optional[LLMProvider] = None
        self._current_provider_name: Optional[str] = None
        self._current_model: Optional[str] = None
        self.on_progress: Optional[Callable[[str], None]] = None
        
        logging.info("InsightsService initialized")
    
    def _get_provider(self) -> LLMProvider:
        """Get or create the LLM provider based on current settings.
        
        Returns:
            An initialized LLMProvider instance.
            
        Raises:
            Exception: If no provider is available.
        """
        # Load settings to check current provider preference
        settings = settings_manager.load_all_settings()
        provider_name = settings.get('insights_provider', 'openai')
        model = settings.get('insights_model')
        
        # Reinitialize if provider changed
        if self._provider is None or self._current_provider_name != provider_name:
            if self._provider:
                self._provider.cleanup()
            
            self._provider = get_llm_provider(provider_name)
            self._current_provider_name = provider_name
            self._current_model = model
            logging.info(f"Insights using provider: {provider_name}")
        
        if not self._provider.is_available():
            raise Exception(
                f"{self._provider.name} is not available. "
                f"Please configure your API key in Settings > Insights."
            )
        
        return self._provider
    
    def _get_model(self) -> Optional[str]:
        """Get the model to use from settings."""
        settings = settings_manager.load_all_settings()
        return settings.get('insights_model')
    
    def _report_progress(self, message: str):
        """Report progress to callback if set."""
        if self.on_progress:
            self.on_progress(message)
        logging.info(f"Insights progress: {message}")
    
    def generate_summary(self, transcript: str) -> str:
        """Generate a summary of the meeting transcript.
        
        Args:
            transcript: The meeting transcript text.
            
        Returns:
            A structured summary of the meeting.
            
        Raises:
            Exception: If generation fails.
        """
        return self._generate_insight(InsightType.SUMMARY, transcript)
    
    def generate_action_items(self, transcript: str) -> str:
        """Extract action items from the meeting transcript.
        
        Args:
            transcript: The meeting transcript text.
            
        Returns:
            A list of action items extracted from the meeting.
            
        Raises:
            Exception: If generation fails.
        """
        return self._generate_insight(InsightType.ACTION_ITEMS, transcript)
    
    def generate_custom(self, transcript: str, custom_prompt: str) -> str:
        """Generate custom insights based on a user-provided prompt.
        
        Args:
            transcript: The meeting transcript text.
            custom_prompt: The user's specific question or analysis request.
            
        Returns:
            Insights based on the custom prompt.
            
        Raises:
            Exception: If generation fails.
        """
        return self._generate_insight(InsightType.CUSTOM, transcript, custom_prompt)
    
    def _generate_insight(
        self, 
        insight_type: InsightType, 
        transcript: str,
        custom_prompt: str = ""
    ) -> str:
        """Generate insights of a specific type.
        
        Args:
            insight_type: The type of insight to generate.
            transcript: The meeting transcript text.
            custom_prompt: Custom prompt for CUSTOM insight type.
            
        Returns:
            Generated insight text.
            
        Raises:
            Exception: If generation fails.
        """
        if not transcript or not transcript.strip():
            raise ValueError("Transcript is empty. Cannot generate insights.")
        
        self._report_progress(f"Initializing {insight_type.value} generation...")
        
        provider = self._get_provider()
        model = self._get_model()
        
        # Get prompt templates
        templates = PROMPT_TEMPLATES[insight_type]
        system_prompt = templates["system"]
        user_template = templates["user"]
        
        # Format user prompt
        if insight_type == InsightType.CUSTOM:
            user_prompt = user_template.format(
                transcript=transcript,
                custom_prompt=custom_prompt
            )
        else:
            user_prompt = user_template.format(transcript=transcript)
        
        self._report_progress(f"Generating {insight_type.value} with {provider.name}...")
        
        try:
            result = provider.generate_completion(
                prompt=user_prompt,
                system_prompt=system_prompt,
                model=model,
                max_tokens=3000,
                temperature=0.5  # Lower temperature for more focused output
            )
            
            self._report_progress("Generation complete!")
            return result
            
        except Exception as e:
            self._report_progress(f"Generation failed: {str(e)}")
            raise
    
    def is_available(self) -> bool:
        """Check if the insights service is available.
        
        Returns:
            True if at least one provider is configured and available.
        """
        try:
            provider = self._get_provider()
            return provider.is_available()
        except Exception:
            return False
    
    def get_available_providers(self) -> Dict[str, bool]:
        """Get availability status for all providers.
        
        Returns:
            Dictionary mapping provider names to availability status.
        """
        availability = {}
        
        for provider_name in ["openai", "openrouter"]:
            try:
                provider = get_llm_provider(provider_name)
                availability[provider_name] = provider.is_available()
                provider.cleanup()
            except Exception:
                availability[provider_name] = False
        
        return availability
    
    def cancel(self):
        """Cancel the current insight generation."""
        if self._provider:
            self._provider.cancel()

    # -------------------------------------------------------------------------
    # Persistence Methods
    # -------------------------------------------------------------------------

    def save_insight(
        self,
        meeting_id: str,
        insight_type: InsightType,
        content: str,
        custom_prompt: Optional[str] = None
    ) -> int:
        """Save an insight to the database.

        Args:
            meeting_id: Meeting ID.
            insight_type: Type of insight.
            content: The generated insight content.
            custom_prompt: Custom prompt (only for CUSTOM type).

        Returns:
            The row ID of the saved insight.
        """
        # Get current provider and model info
        settings = settings_manager.load_all_settings()
        provider = settings.get('insights_provider', 'openai')
        model = settings.get('insights_model')

        return db.save_insight(
            meeting_id=meeting_id,
            insight_type=insight_type.value,
            content=content,
            custom_prompt=custom_prompt,
            provider=provider,
            model=model
        )

    def get_saved_insight(
        self,
        meeting_id: str,
        insight_type: InsightType,
        custom_prompt: Optional[str] = None
    ) -> Optional[InsightEntry]:
        """Get a saved insight from the database.

        Args:
            meeting_id: Meeting ID.
            insight_type: Type of insight.
            custom_prompt: Custom prompt (only for CUSTOM type).

        Returns:
            InsightEntry or None if not found.
        """
        data = db.get_insight(
            meeting_id=meeting_id,
            insight_type=insight_type.value,
            custom_prompt=custom_prompt
        )
        return InsightEntry.from_dict(data) if data else None

    def get_all_saved_insights(self, meeting_id: str) -> List[InsightEntry]:
        """Get all saved insights for a meeting.

        Args:
            meeting_id: Meeting ID.

        Returns:
            List of InsightEntry objects.
        """
        data_list = db.get_all_insights(meeting_id)
        return [InsightEntry.from_dict(data) for data in data_list]

    def meeting_has_insights(self, meeting_id: str) -> bool:
        """Check if a meeting has any saved insights.

        Args:
            meeting_id: Meeting ID.

        Returns:
            True if the meeting has at least one saved insight.
        """
        return db.has_insights(meeting_id)

    def cleanup(self):
        """Clean up service resources."""
        if self._provider:
            self._provider.cleanup()
            self._provider = None
        self._current_provider_name = None
        self._current_model = None
        logging.info("InsightsService cleaned up")


# Global insights service instance
insights_service = InsightsService()
