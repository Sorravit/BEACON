#!/usr/bin/env python3
"""
AI Assistant - OpenAPI Compatible

A production-ready command-line AI assistant that connects to OpenAI-compatible 
API endpoints. Features proper error handling, logging, and configuration management.

Author: AI Assistant
Version: 2.0.0
"""

import os
import sys
import logging
from typing import Optional, List, Dict
from pathlib import Path
from openai import OpenAI
from openai import OpenAIError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('ai_assistant.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class AIAssistantConfig:
    """Configuration management for AI Assistant"""
    
    def __init__(self):
        self.api_key: Optional[str] = None
        self.base_url: str = "https://api.openai.com/v1"
        self.model: str = "gpt-3.5-turbo"
        self.temperature: float = 0.7
        self.max_tokens: int = 1000
        self.load_config()
    
    def load_config(self) -> None:
        """Load configuration from environment variables or .env file"""
        # Try loading from .env file if it exists
        env_file = Path('.env')
        if env_file.exists():
            self._load_env_file(env_file)
        
        # Load from environment variables (overrides .env file)
        self.api_key = os.environ.get('OPENAI_API_KEY')
        self.base_url = os.environ.get('OPENAI_BASE_URL', self.base_url)
        self.model = os.environ.get('AI_MODEL', self.model)
        
        # Optional: Load temperature and max_tokens if provided
        try:
            temp = os.environ.get('AI_TEMPERATURE')
            if temp:
                self.temperature = float(temp)
        except ValueError:
            logger.warning(f"Invalid temperature value, using default: {self.temperature}")
        
        try:
            tokens = os.environ.get('AI_MAX_TOKENS')
            if tokens:
                self.max_tokens = int(tokens)
        except ValueError:
            logger.warning(f"Invalid max_tokens value, using default: {self.max_tokens}")
    
    def _load_env_file(self, env_file: Path) -> None:
        """Load environment variables from .env file"""
        try:
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        if '=' in line:
                            key, value = line.split('=', 1)
                            # Remove quotes if present
                            value = value.strip().strip('"').strip("'")
                            os.environ[key.strip()] = value
            logger.info("Loaded configuration from .env file")
        except Exception as e:
            logger.warning(f"Failed to load .env file: {e}")
    
    def validate(self) -> bool:
        """Validate that required configuration is present"""
        if not self.api_key:
            logger.error("API key is not configured")
            return False
        return True
    
    def display_info(self) -> None:
        """Display non-sensitive configuration information"""
        print(f"Model: {self.model}")
        print(f"Endpoint: {self.base_url}")
        print(f"Temperature: {self.temperature}")
        print(f"Max Tokens: {self.max_tokens}")


class AIAssistant:
    """Main AI Assistant application"""
    
    def __init__(self, config: AIAssistantConfig):
        self.config = config
        self.client: Optional[OpenAI] = None
        self.conversation: List[Dict[str, str]] = []
    
    def initialize(self) -> bool:
        """Initialize the OpenAI client"""
        try:
            self.client = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url
            )
            logger.info("AI Assistant initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize AI Assistant: {e}")
            return False
    
    def add_message(self, role: str, content: str) -> None:
        """Add a message to the conversation history"""
        if not content.strip():
            return
        self.conversation.append({
            "role": role,
            "content": content
        })
    
    def clear_conversation(self) -> None:
        """Clear the conversation history"""
        self.conversation.clear()
        logger.info("Conversation history cleared")
    
    def get_response(self, user_input: str) -> Optional[str]:
        """Get AI response for user input"""
        if not self.client:
            logger.error("Client not initialized")
            return None
        
        try:
            # Add user message to conversation
            self.add_message("user", user_input)
            
            # Call API
            logger.debug(f"Sending request to API with {len(self.conversation)} messages")
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=self.conversation,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens
            )
            
            # Extract assistant response
            assistant_message = response.choices[0].message.content
            
            # Add to conversation history
            self.add_message("assistant", assistant_message)
            
            logger.info("Successfully received AI response")
            return assistant_message
            
        except OpenAIError as e:
            logger.error(f"OpenAI API error: {e}")
            # Remove the last user message since we failed to get a response
            if self.conversation and self.conversation[-1]["role"] == "user":
                self.conversation.pop()
            return None
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            # Remove the last user message since we failed to get a response
            if self.conversation and self.conversation[-1]["role"] == "user":
                self.conversation.pop()
            return None
    
    def run(self) -> None:
        """Run the interactive assistant loop"""
        self._print_header()
        
        while True:
            try:
                # Get user input
                user_input = input("\n👤 You: ").strip()
                
                if not user_input:
                    continue
                
                # Check for exit commands
                if user_input.lower() in ['quit', 'exit', 'q']:
                    print("\n👋 Goodbye!")
                    logger.info("User exited the application")
                    break
                
                # Check for special commands
                if user_input.lower() in ['clear', 'reset']:
                    self.clear_conversation()
                    print("🔄 Conversation history cleared!")
                    continue
                
                if user_input.lower() == 'help':
                    self._print_help()
                    continue
                
                # Get AI response
                print("\n🤔 AI is thinking...\n")
                response = self.get_response(user_input)
                
                if response:
                    print(f"🤖 AI: {response}")
                else:
                    print("❌ Failed to get response. Please check the logs and try again.")
                
            except KeyboardInterrupt:
                print("\n\n👋 Goodbye!")
                logger.info("User interrupted the application")
                break
            except EOFError:
                print("\n\n👋 Goodbye!")
                logger.info("EOF received, exiting")
                break
            except Exception as e:
                logger.error(f"Unexpected error in main loop: {e}")
                print(f"\n❌ An unexpected error occurred. Please check the logs.")
    
    def _print_header(self) -> None:
        """Print application header"""
        print("=" * 60)
        print("🤖 AI Assistant - OpenAPI Compatible (v2.0.0)")
        print("=" * 60)
        self.config.display_info()
        print("\nType your question, 'help' for commands, or 'quit' to exit")
        print("-" * 60)
    
    def _print_help(self) -> None:
        """Print help information"""
        print("\n📖 Available Commands:")
        print("  quit, exit, q  - Exit the application")
        print("  clear, reset   - Clear conversation history")
        print("  help           - Show this help message")
        print("  Ctrl+C         - Interrupt and exit")


def print_config_help() -> None:
    """Print configuration help message"""
    print("❌ Error: API key is not configured\n")
    print("Please set your API key using one of these methods:\n")
    print("1. Create a .env file in the current directory:")
    print("   OPENAI_API_KEY=your-api-key-here")
    print("   OPENAI_BASE_URL=https://your-endpoint-url")
    print("   AI_MODEL=your-model-name\n")
    print("2. Set environment variables:")
    print("   export OPENAI_API_KEY='your-api-key-here'")
    print("   export OPENAI_BASE_URL='https://your-endpoint-url'")
    print("   export AI_MODEL='your-model-name'\n")
    print("3. Copy config.example.env to .env and edit it:\n")
    print("   cp config.example.env .env")
    print("   # Edit .env with your values\n")
    print("For more information, see README_AI_ASSISTANT.md")


def main() -> int:
    """Main entry point"""
    try:
        # Load configuration
        config = AIAssistantConfig()
        
        # Validate configuration
        if not config.validate():
            print_config_help()
            return 1
        
        # Initialize assistant
        assistant = AIAssistant(config)
        if not assistant.initialize():
            print("\n❌ Failed to initialize AI Assistant. Please check your configuration.")
            return 1
        
        # Run interactive loop
        assistant.run()
        
        return 0
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        print(f"\n❌ Fatal error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())