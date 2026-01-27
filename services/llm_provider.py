"""
LLM Provider service for generating insights from meeting transcriptions.
Supports OpenAI and OpenRouter (OpenAI-compatible API).
"""
import os
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from openai import OpenAI

from config import config


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""
    
    def __init__(self):
        self.client: Optional[OpenAI] = None
        self._is_generating = False
        self._should_cancel = False
    
    @abstractmethod
    def generate_completion(
        self, 
        prompt: str, 
        system_prompt: str = "",
        model: Optional[str] = None,
        max_tokens: int = 2000,
        temperature: float = 0.7
    ) -> str:
        """Generate a completion from the LLM.
        
        Args:
            prompt: The user prompt/message.
            system_prompt: Optional system prompt for context.
            model: Model to use (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            
        Returns:
            The generated text response.
            
        Raises:
            Exception: If generation fails.
        """
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if the provider is available (API key set, client initialized).
        
        Returns:
            True if provider is ready to use.
        """
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Get the provider name."""
        pass
    
    @property
    @abstractmethod
    def default_model(self) -> str:
        """Get the default model for this provider."""
        pass
    
    def cancel(self):
        """Request cancellation of current generation."""
        self._should_cancel = True
    
    def reset_cancel_flag(self):
        """Reset the cancellation flag."""
        self._should_cancel = False
    
    @property
    def is_generating(self) -> bool:
        """Check if generation is in progress."""
        return self._is_generating
    
    def cleanup(self):
        """Clean up provider resources."""
        if self.client is not None:
            try:
                self.client.close()
            except Exception as e:
                logging.debug(f"Error closing LLM client: {e}")
            self.client = None


class OpenAILLMProvider(LLMProvider):
    """OpenAI LLM provider for chat completions."""

    DEFAULT_MODEL = "gpt-5-nano"

    # Models that don't support temperature parameter (reasoning models)
    NO_TEMPERATURE_MODELS = frozenset([
        "o1", "o1-mini", "o1-preview",
        "o3", "o3-mini",
        "gpt-5-nano",  # Newer lightweight model without temperature support
    ])
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize the OpenAI LLM provider.
        
        Args:
            api_key: OpenAI API key. Uses settings or environment variable if None.
        """
        super().__init__()
        self.api_key = api_key or self._get_api_key()
        self._initialize_client()
    
    def _get_api_key(self) -> Optional[str]:
        """Get API key from settings, environment variables, or .env file."""
        # First try settings manager
        try:
            from services.settings import settings_manager
            key = settings_manager.get_insights_api_key('openai')
            if key:
                return key
        except Exception as e:
            logging.debug(f"Could not get key from settings: {e}")
        
        # Fall back to environment variable
        api_key = os.getenv('OPENAI_API_KEY')
        
        if not api_key:
            try:
                from dotenv import load_dotenv
                env_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), 
                    '..', 
                    config.ENV_FILE
                )
                load_dotenv(env_path)
                api_key = os.getenv('OPENAI_API_KEY')
            except ImportError:
                logging.debug("python-dotenv not installed")
            except Exception as e:
                logging.warning(f"Failed to load .env file: {e}")
        
        return api_key
    
    def _initialize_client(self):
        """Initialize the OpenAI client."""
        if self.api_key:
            try:
                self.client = OpenAI(api_key=self.api_key)
                logging.info("OpenAI LLM provider initialized successfully")
            except Exception as e:
                logging.error(f"Failed to initialize OpenAI LLM client: {e}")
                self.client = None
        else:
            logging.debug("No OpenAI API key found for LLM provider")
            self.client = None

    def _model_excludes_temperature(self, model: str) -> bool:
        """Check if a model doesn't support the temperature parameter.

        Args:
            model: The model name to check.

        Returns:
            True if temperature should be excluded for this model.
        """
        # Check exact match first
        if model in self.NO_TEMPERATURE_MODELS:
            return True
        # Check if model name starts with any known prefix (handles versioned models)
        for prefix in self.NO_TEMPERATURE_MODELS:
            if model.startswith(prefix):
                return True
        return False

    def generate_completion(
        self, 
        prompt: str, 
        system_prompt: str = "",
        model: Optional[str] = None,
        max_tokens: int = 2000,
        temperature: float = 0.7
    ) -> str:
        """Generate a completion using OpenAI's Responses API.
        
        Args:
            prompt: The user prompt/message.
            system_prompt: Optional system prompt for context.
            model: Model to use (defaults to gpt-4o).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            
        Returns:
            The generated text response.
            
        Raises:
            Exception: If generation fails or provider unavailable.
        """
        if not self.is_available():
            raise Exception("OpenAI LLM provider is not available (no API key)")
        
        try:
            self._is_generating = True
            self.reset_cancel_flag()
            
            model = model or self.DEFAULT_MODEL
            
            # Build input for Responses API
            input_content = []
            if system_prompt:
                input_content.append({
                    "role": "system",
                    "content": system_prompt
                })
            input_content.append({
                "role": "user", 
                "content": prompt
            })
            
            logging.info(f"Generating response with OpenAI model: {model}")

            # Build API request parameters
            api_params = {
                "model": model,
                "input": input_content,
                "max_output_tokens": max_tokens,
                "store": False,  # Stateless - don't persist conversation
            }

            # Only add temperature for models that support it
            # Reasoning models (o1, o3 series) and gpt-5-nano don't support temperature
            if not self._model_excludes_temperature(model):
                api_params["temperature"] = temperature

            # Use Responses API (the new recommended API)
            response = self.client.responses.create(**api_params)
            
            if self._should_cancel:
                raise Exception("Generation cancelled by user")
            
            # Extract text from output_text (Responses API format)
            result = response.output_text.strip()
            logging.info(f"OpenAI response generated: {len(result)} characters")
            
            return result
            
        except Exception as e:
            logging.error(f"OpenAI completion failed: {e}")
            raise
        finally:
            self._is_generating = False
    
    def is_available(self) -> bool:
        """Check if the OpenAI provider is available."""
        return self.client is not None and self.api_key is not None
    
    @property
    def name(self) -> str:
        return "OpenAI"
    
    @property
    def default_model(self) -> str:
        return self.DEFAULT_MODEL
    
    def update_api_key(self, api_key: str):
        """Update the API key and reinitialize the client."""
        self.api_key = api_key
        self._initialize_client()


class OpenRouterProvider(LLMProvider):
    """OpenRouter LLM provider - OpenAI-compatible API with access to many models."""
    
    BASE_URL = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL = "anthropic/claude-3.5-sonnet"
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize the OpenRouter provider.
        
        Args:
            api_key: OpenRouter API key. Uses settings or environment variable if None.
        """
        super().__init__()
        self.api_key = api_key or self._get_api_key()
        self._initialize_client()
    
    def _get_api_key(self) -> Optional[str]:
        """Get API key from settings, environment variables, or .env file."""
        # First try settings manager
        try:
            from services.settings import settings_manager
            key = settings_manager.get_insights_api_key('openrouter')
            if key:
                return key
        except Exception as e:
            logging.debug(f"Could not get key from settings: {e}")
        
        # Fall back to environment variable
        api_key = os.getenv('OPENROUTER_API_KEY')
        
        if not api_key:
            try:
                from dotenv import load_dotenv
                env_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), 
                    '..', 
                    config.ENV_FILE
                )
                load_dotenv(env_path)
                api_key = os.getenv('OPENROUTER_API_KEY')
            except ImportError:
                logging.debug("python-dotenv not installed")
            except Exception as e:
                logging.warning(f"Failed to load .env file: {e}")
        
        return api_key
    
    def _initialize_client(self):
        """Initialize the OpenRouter client (uses OpenAI SDK with custom base_url)."""
        if self.api_key:
            try:
                self.client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.BASE_URL
                )
                logging.info("OpenRouter LLM provider initialized successfully")
            except Exception as e:
                logging.error(f"Failed to initialize OpenRouter client: {e}")
                self.client = None
        else:
            logging.debug("No OpenRouter API key found")
            self.client = None
    
    def generate_completion(
        self, 
        prompt: str, 
        system_prompt: str = "",
        model: Optional[str] = None,
        max_tokens: int = 2000,
        temperature: float = 0.7
    ) -> str:
        """Generate a completion using OpenRouter's Responses API.
        
        Args:
            prompt: The user prompt/message.
            system_prompt: Optional system prompt for context.
            model: Model to use (e.g., "anthropic/claude-3.5-sonnet").
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            
        Returns:
            The generated text response.
            
        Raises:
            Exception: If generation fails or provider unavailable.
        """
        if not self.is_available():
            raise Exception("OpenRouter provider is not available (no API key)")
        
        try:
            self._is_generating = True
            self.reset_cancel_flag()
            
            model = model or self.DEFAULT_MODEL
            
            # Build input for Responses API
            input_content = []
            if system_prompt:
                input_content.append({
                    "role": "system",
                    "content": system_prompt
                })
            input_content.append({
                "role": "user", 
                "content": prompt
            })
            
            logging.info(f"Generating response with OpenRouter model: {model}")
            
            # Use Responses API (OpenRouter also supports it)
            response = self.client.responses.create(
                model=model,
                input=input_content,
                max_output_tokens=max_tokens,
                temperature=temperature,
                store=False,  # Stateless
                extra_headers={
                    "HTTP-Referer": "https://github.com/openwhisper",
                    "X-Title": "OpenWhisper Meeting Insights"
                }
            )
            
            if self._should_cancel:
                raise Exception("Generation cancelled by user")
            
            # Extract text from output_text (Responses API format)
            result = response.output_text.strip()
            logging.info(f"OpenRouter response generated: {len(result)} characters")
            
            return result
            
        except Exception as e:
            logging.error(f"OpenRouter response failed: {e}")
            raise
        finally:
            self._is_generating = False
    
    def is_available(self) -> bool:
        """Check if the OpenRouter provider is available."""
        return self.client is not None and self.api_key is not None
    
    @property
    def name(self) -> str:
        return "OpenRouter"
    
    @property
    def default_model(self) -> str:
        return self.DEFAULT_MODEL
    
    def update_api_key(self, api_key: str):
        """Update the API key and reinitialize the client."""
        self.api_key = api_key
        self._initialize_client()


def get_llm_provider(provider_name: str = "openai", api_key: Optional[str] = None) -> LLMProvider:
    """Factory function to get an LLM provider by name.
    
    Args:
        provider_name: Name of the provider ("openai" or "openrouter").
        api_key: Optional API key (uses environment variable if None).
        
    Returns:
        An initialized LLMProvider instance.
        
    Raises:
        ValueError: If provider name is not recognized.
    """
    provider_name = provider_name.lower()
    
    if provider_name == "openai":
        return OpenAILLMProvider(api_key=api_key)
    elif provider_name == "openrouter":
        return OpenRouterProvider(api_key=api_key)
    else:
        raise ValueError(f"Unknown LLM provider: {provider_name}. Valid options: openai, openrouter")
