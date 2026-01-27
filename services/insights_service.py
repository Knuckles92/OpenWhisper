"""
Insights Service for generating meeting insights from transcriptions.
Uses LLM providers (OpenAI/OpenRouter) to analyze meeting transcripts.
"""
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Callable, Dict, Any, List
from enum import Enum

from services.llm_provider import LLMProvider, get_llm_provider
from services.settings import settings_manager
from services.database import db
from config import config


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


@dataclass
class InsightGenerationOptions:
    """Options for customizing insight generation."""

    # Output style
    output_length: str = "standard"  # brief, standard, detailed
    formatting_style: str = "bullet_points"  # bullet_points, narrative, numbered_list, markdown
    tone: str = "professional"  # professional, casual, technical, executive

    # Focus areas (list of enabled areas)
    focus_areas: List[str] = field(default_factory=list)  # decisions, discussions, technical, people, timelines, risks

    # Filters
    participant_filter: Optional[str] = None  # comma-separated names
    topic_filter: Optional[str] = None  # comma-separated keywords

    # Advanced
    creativity: float = 0.5  # 0.0 to 1.0 (maps to temperature)
    language: str = "english"
    include_timestamps: bool = False
    include_speaker_attribution: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage/serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'InsightGenerationOptions':
        """Create from dictionary."""
        # Filter to only valid fields
        valid_fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid_fields)

    @classmethod
    def get_defaults(cls) -> 'InsightGenerationOptions':
        """Get default options."""
        return cls()


@dataclass
class InsightPreset:
    """A saved preset for insight generation."""
    id: str
    name: str
    insight_type: str  # summary, action_items, custom
    options: InsightGenerationOptions
    custom_prompt: Optional[str] = None  # Only for custom type
    is_builtin: bool = False
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "id": self.id,
            "name": self.name,
            "insight_type": self.insight_type,
            "options": self.options.to_dict(),
            "custom_prompt": self.custom_prompt,
            "is_builtin": self.is_builtin,
            "created_at": self.created_at
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'InsightPreset':
        """Create from dictionary."""
        options_data = data.get("options", {})
        options = InsightGenerationOptions.from_dict(options_data) if options_data else InsightGenerationOptions()
        return cls(
            id=data["id"],
            name=data["name"],
            insight_type=data.get("insight_type", "summary"),
            options=options,
            custom_prompt=data.get("custom_prompt"),
            is_builtin=data.get("is_builtin", False),
            created_at=data.get("created_at")
        )


class InsightPromptBuilder:
    """Builds customized prompts based on user options."""

    def __init__(self, options: InsightGenerationOptions):
        self.options = options

    def build_system_prompt(self, insight_type: InsightType) -> str:
        """Build a customized system prompt based on options."""
        # Base system prompts for each type
        base_prompts = {
            InsightType.SUMMARY: (
                "You are an expert meeting analyst. Your task is to analyze meeting "
                "transcripts and provide clear, concise summaries that capture the "
                "essential information discussed."
            ),
            InsightType.ACTION_ITEMS: (
                "You are an expert at extracting action items and tasks from meeting "
                "transcripts. Your goal is to identify all commitments, tasks, and "
                "follow-up items mentioned during the meeting."
            ),
            InsightType.CUSTOM: (
                "You are an expert meeting analyst who can provide various types of "
                "analysis on meeting transcripts based on specific requests."
            )
        }

        prompt = base_prompts.get(insight_type, base_prompts[InsightType.SUMMARY])

        # Add output style instructions
        prompt += "\n\n**Output Style Requirements:**"
        prompt += f"\n- Length: {self._get_length_instruction()}"
        prompt += f"\n- Format: {self._get_format_instruction()}"
        prompt += f"\n- Tone: {self._get_tone_instruction()}"

        # Add focus area instructions if specified
        if self.options.focus_areas:
            focus_instructions = self._get_focus_instructions()
            if focus_instructions:
                prompt += f"\n\n**Priority Focus Areas:**\n{focus_instructions}"

        # Add language instruction if not English
        if self.options.language.lower() != "english":
            prompt += f"\n\n**Language:** Respond entirely in {self.options.language.title()}."

        # Add attribution instructions
        if self.options.include_speaker_attribution:
            prompt += "\n\n**Speaker Attribution:** Attribute key points, quotes, and contributions to specific speakers when mentioned in the transcript."

        if self.options.include_timestamps:
            prompt += "\n\n**Timestamps:** Include approximate timestamps for key points when discernible from the transcript context."

        return prompt

    def build_user_prompt(
        self,
        insight_type: InsightType,
        transcript: str,
        custom_prompt: str = ""
    ) -> str:
        """Build a customized user prompt based on options."""
        prompt = f"Analyze the following meeting transcript:\n\n---\nTRANSCRIPT:\n{transcript}\n---\n\n"

        # Add filter instructions if specified
        if self.options.participant_filter:
            participants = self.options.participant_filter.strip()
            prompt += f"**Focus on contributions from:** {participants}\n\n"

        if self.options.topic_filter:
            topics = self.options.topic_filter.strip()
            prompt += f"**Pay special attention to topics related to:** {topics}\n\n"

        # Add type-specific instructions
        if insight_type == InsightType.SUMMARY:
            prompt += self._get_summary_instructions()
        elif insight_type == InsightType.ACTION_ITEMS:
            prompt += self._get_action_items_instructions()
        elif insight_type == InsightType.CUSTOM and custom_prompt:
            prompt += f"**Specific Request:** {custom_prompt}"

        return prompt

    def _get_length_instruction(self) -> str:
        """Get instruction for output length."""
        length_opts = config.INSIGHT_LENGTH_OPTIONS
        if self.options.output_length in length_opts:
            return length_opts[self.options.output_length]["instruction"]
        return length_opts["standard"]["instruction"]

    def _get_format_instruction(self) -> str:
        """Get instruction for output format."""
        format_opts = config.INSIGHT_FORMAT_OPTIONS
        if self.options.formatting_style in format_opts:
            return format_opts[self.options.formatting_style]["instruction"]
        return format_opts["bullet_points"]["instruction"]

    def _get_tone_instruction(self) -> str:
        """Get instruction for tone."""
        tone_opts = config.INSIGHT_TONE_OPTIONS
        if self.options.tone in tone_opts:
            return tone_opts[self.options.tone]["instruction"]
        return tone_opts["professional"]["instruction"]

    def _get_focus_instructions(self) -> str:
        """Get instructions for focus areas."""
        focus_opts = config.INSIGHT_FOCUS_AREAS
        instructions = []
        for area in self.options.focus_areas:
            if area in focus_opts:
                instructions.append(f"- {focus_opts[area]['instruction']}")
        return "\n".join(instructions) if instructions else ""

    def _get_summary_instructions(self) -> str:
        """Get summary-specific instructions."""
        return (
            "Provide a well-structured summary that includes:\n"
            "1. **Overview**: A brief 2-3 sentence summary of the meeting\n"
            "2. **Key Topics Discussed**: Main subjects covered\n"
            "3. **Decisions Made**: Any decisions or agreements reached\n"
            "4. **Notable Points**: Important insights or information shared\n"
            "5. **Next Steps**: Any mentioned follow-up actions (if applicable)"
        )

    def _get_action_items_instructions(self) -> str:
        """Get action items-specific instructions."""
        return (
            "Extract all action items and tasks. For each item include:\n"
            "- **Task**: Description of what needs to be done\n"
            "- **Owner**: Who is responsible (if mentioned, otherwise 'Unassigned')\n"
            "- **Deadline**: When it's due (if mentioned, otherwise 'Not specified')\n"
            "- **Priority**: High/Medium/Low (based on context)\n\n"
            "If no clear action items were discussed, indicate that the meeting was primarily informational."
        )

    def get_max_tokens(self) -> int:
        """Get max tokens based on output length option."""
        length_opts = config.INSIGHT_LENGTH_OPTIONS
        if self.options.output_length in length_opts:
            return length_opts[self.options.output_length]["max_tokens"]
        return length_opts["standard"]["max_tokens"]

    def get_temperature(self) -> float:
        """Get temperature based on creativity setting."""
        # Clamp creativity to valid range
        return max(0.0, min(1.0, self.options.creativity))


# Prompt templates for different insight types (kept for backward compatibility)
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
    
    def generate_summary(
        self,
        transcript: str,
        options: Optional[InsightGenerationOptions] = None
    ) -> str:
        """Generate a summary of the meeting transcript.

        Args:
            transcript: The meeting transcript text.
            options: Optional generation options for customization.

        Returns:
            A structured summary of the meeting.

        Raises:
            Exception: If generation fails.
        """
        return self._generate_insight(InsightType.SUMMARY, transcript, options=options)

    def generate_action_items(
        self,
        transcript: str,
        options: Optional[InsightGenerationOptions] = None
    ) -> str:
        """Extract action items from the meeting transcript.

        Args:
            transcript: The meeting transcript text.
            options: Optional generation options for customization.

        Returns:
            A list of action items extracted from the meeting.

        Raises:
            Exception: If generation fails.
        """
        return self._generate_insight(InsightType.ACTION_ITEMS, transcript, options=options)

    def generate_custom(
        self,
        transcript: str,
        custom_prompt: str,
        options: Optional[InsightGenerationOptions] = None
    ) -> str:
        """Generate custom insights based on a user-provided prompt.

        Args:
            transcript: The meeting transcript text.
            custom_prompt: The user's specific question or analysis request.
            options: Optional generation options for customization.

        Returns:
            Insights based on the custom prompt.

        Raises:
            Exception: If generation fails.
        """
        return self._generate_insight(InsightType.CUSTOM, transcript, custom_prompt, options=options)
    
    def _generate_insight(
        self,
        insight_type: InsightType,
        transcript: str,
        custom_prompt: str = "",
        options: Optional[InsightGenerationOptions] = None
    ) -> str:
        """Generate insights of a specific type.

        Args:
            insight_type: The type of insight to generate.
            transcript: The meeting transcript text.
            custom_prompt: Custom prompt for CUSTOM insight type.
            options: Optional generation options for customization.

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

        # Use InsightPromptBuilder if options provided, otherwise fall back to templates
        if options is not None:
            # Use dynamic prompt building with options
            prompt_builder = InsightPromptBuilder(options)
            system_prompt = prompt_builder.build_system_prompt(insight_type)
            user_prompt = prompt_builder.build_user_prompt(insight_type, transcript, custom_prompt)
            max_tokens = prompt_builder.get_max_tokens()
            temperature = prompt_builder.get_temperature()
        else:
            # Fall back to legacy templates for backward compatibility
            templates = PROMPT_TEMPLATES[insight_type]
            system_prompt = templates["system"]
            user_template = templates["user"]

            if insight_type == InsightType.CUSTOM:
                user_prompt = user_template.format(
                    transcript=transcript,
                    custom_prompt=custom_prompt
                )
            else:
                user_prompt = user_template.format(transcript=transcript)

            max_tokens = config.INSIGHTS_MAX_TOKENS
            temperature = config.INSIGHTS_TEMPERATURE

        self._report_progress(f"Generating {insight_type.value} with {provider.name}...")

        try:
            result = provider.generate_completion(
                prompt=user_prompt,
                system_prompt=system_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature
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
