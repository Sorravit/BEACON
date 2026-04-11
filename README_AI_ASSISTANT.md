# AI Assistant - OpenAPI Compatible (v2.0.0)

A production-ready command-line AI assistant application that connects to OpenAI-compatible API endpoints to answer questions and perform tasks.

## 🌟 Features

- 🤖 Connect to any OpenAI-compatible API endpoint
- 💬 Interactive conversation with context memory
- 🔧 Flexible configuration via `.env` files or environment variables
- 🔒 Secure credential management (no hardcoded secrets)
- 📝 Comprehensive logging for debugging and monitoring
- 🛡️ Robust error handling and recovery
- 🎯 Type hints and clean code architecture
- 🌐 Works with OpenAI, Claude, local LLMs, and other compatible services

## 📋 Requirements

- Python 3.7 or higher
- OpenAI Python library (>= 1.0.0)

## 🚀 Quick Start

### 1. Set Up Virtual Environment (Recommended)

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Your API Credentials

**Option A: Using .env file (Recommended)**

```bash
# Copy the example configuration
cp config.example.env .env

# Edit .env with your credentials
nano .env  # or use your favorite editor
```

**Option B: Using Environment Variables**

```bash
export OPENAI_API_KEY='your-api-key-here'
export OPENAI_BASE_URL='https://your-endpoint-url'
export AI_MODEL='your-model-name'
```

### 4. Run the Assistant

```bash
# If using virtual environment
venv/bin/python3 ai_assistant.py

# Or if you activated the virtual environment
python3 ai_assistant.py
```

## ⚙️ Configuration

The application supports multiple configuration methods, loaded in this priority order:

1. Environment variables (highest priority)
2. `.env` file in the current directory
3. Default values (lowest priority)

### Configuration Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENAI_API_KEY` | Yes | - | Your API key |
| `OPENAI_BASE_URL` | No | `https://api.openai.com/v1` | API endpoint URL |
| `AI_MODEL` | No | `gpt-3.5-turbo` | Model to use |
| `AI_TEMPERATURE` | No | `0.7` | Response creativity (0.0-2.0) |
| `AI_MAX_TOKENS` | No | `1000` | Maximum response length |

### Example .env File

```env
# IBM ICA Configuration
OPENAI_API_KEY=your-ica-api-key-here
OPENAI_BASE_URL=https://sg.ica.ibm.com/ica/apis/v3
AI_MODEL=global/anthropic.claude-sonnet-4-5-20250929-v1:0
AI_TEMPERATURE=0.7
AI_MAX_TOKENS=1000
```

## 📖 Usage

### Interactive Commands

Once the assistant is running, you can use these commands:

- **Regular text**: Ask questions or give tasks to the AI
- `help`: Show available commands
- `clear` or `reset`: Clear conversation history
- `quit`, `exit`, or `q`: Exit the application
- `Ctrl+C`: Interrupt and exit

### Example Session

```
============================================================
🤖 AI Assistant - OpenAPI Compatible (v2.0.0)
============================================================
Model: global/anthropic.claude-sonnet-4-5-20250929-v1:0
Endpoint: https://sg.ica.ibm.com/ica/apis/v3
Temperature: 0.7
Max Tokens: 1000

Type your question, 'help' for commands, or 'quit' to exit
------------------------------------------------------------

👤 You: What is the capital of France?

🤔 AI is thinking...

🤖 AI: The capital of France is Paris.

👤 You: Tell me an interesting fact about it

🤔 AI is thinking...

🤖 AI: Paris is known as the "City of Light" (La Ville Lumière)...

👤 You: clear

🔄 Conversation history cleared!

👤 You: quit

👋 Goodbye!
```

## 🔌 Supported Platforms

This application works with any OpenAI-compatible API, including:

### Cloud Services
- **OpenAI** - ChatGPT models (GPT-3.5, GPT-4, etc.)
- **Anthropic** - Claude models (via compatibility layer)
- **IBM ICA** - Claude Sonnet and other models
- **Azure OpenAI** - Microsoft's OpenAI service
- **Together AI** - Various open-source models
- **Groq** - Fast inference endpoints

### Local LLMs
- **LM Studio** - Local model hosting
- **Ollama** - Easy local model management
- **LocalAI** - Self-hosted OpenAI-compatible API
- **text-generation-webui** - Gradio-based interface with API

## 📚 Configuration Examples

### OpenAI (Cloud)

```env
OPENAI_API_KEY=sk-your-openai-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4
```

### IBM ICA (Cloud)

```env
OPENAI_API_KEY=your-ica-api-key
OPENAI_BASE_URL=https://sg.ica.ibm.com/ica/apis/v3
AI_MODEL=global/anthropic.claude-sonnet-4-5-20250929-v1:0
```

### LM Studio (Local)

```env
OPENAI_API_KEY=lm-studio
OPENAI_BASE_URL=http://localhost:1234/v1
AI_MODEL=local-model
```

### Ollama (Local)

```env
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://localhost:11434/v1
AI_MODEL=llama2
```

## 📁 Project Structure

```
.
├── ai_assistant.py          # Main application (production-ready)
├── test_ai_assistant.py     # Test script
├── requirements.txt         # Python dependencies
├── .env                     # Your configuration (NOT in git)
├── config.example.env       # Example configuration
├── .gitignore              # Git ignore rules
├── README_AI_ASSISTANT.md  # This file
└── ai_assistant.log        # Log file (auto-generated)
```

## 🔒 Security Best Practices

1. **Never commit `.env` files** - They contain sensitive credentials
2. **Use `.gitignore`** - Already configured to exclude sensitive files
3. **Rotate API keys regularly** - Update your `.env` file when needed
4. **Use virtual environments** - Isolate dependencies
5. **Review logs carefully** - They may contain sensitive data
6. **Set appropriate file permissions** - `chmod 600 .env` on Unix systems

## 🐛 Troubleshooting

### API Key Not Configured

**Error**: `❌ Error: API key is not configured`

**Solution**: Create a `.env` file or set environment variables:
```bash
cp config.example.env .env
# Edit .env with your credentials
```

### Connection Errors

**Error**: Connection timeout or refused

**Solutions**:
- Check your internet connection
- Verify the API endpoint URL is correct
- For local servers, ensure they're running
- Check firewall settings

### Model Not Found

**Error**: `404 - No available model was found`

**Solution**: Verify the model name in your `.env` file matches an available model:
```env
# Check your provider's documentation for correct model names
AI_MODEL=correct-model-name
```

### Import Errors

**Error**: `ModuleNotFoundError: No module named 'openai'`

**Solution**: Install dependencies in virtual environment:
```bash
venv/bin/pip install -r requirements.txt
```

## 📊 Logging

The application logs to two locations:
- **Console**: Important messages and errors
- **File**: `ai_assistant.log` - Detailed logs including debug information

To change the log level, edit `ai_assistant.py`:
```python
logging.basicConfig(
    level=logging.DEBUG,  # Change to DEBUG for more detail
    # ...
)
```

## 🧪 Testing

Run the test script to verify your configuration:

```bash
venv/bin/python3 test_ai_assistant.py
```

## 🔄 Version History

### v2.0.0 (2026-04-11)
- ✨ Production-ready with secure credential management
- 🔒 Removed hardcoded API keys
- 📝 Added comprehensive logging
- 🛡️ Enhanced error handling
- 🎯 Added type hints
- ⚙️ Improved configuration management with .env support
- 📖 Added clear and help commands
- 🧹 Code refactoring for maintainability

### v1.0.0 (Initial)
- Basic functionality with environment variable configuration

## 🤝 Contributing

This is a production tool. Key principles:
- Never commit sensitive data
- Follow PEP 8 style guidelines
- Add type hints to new functions
- Update documentation
- Test changes thoroughly

## 📄 License

This is a production tool for internal use. Use responsibly and in accordance with your AI provider's terms of service.

## 🆘 Support

For issues or questions:
1. Check the troubleshooting section
2. Review logs in `ai_assistant.log`
3. Verify your `.env` configuration
4. Check your API provider's documentation

## 🎯 Production Deployment Tips

1. **Use environment variables** for secrets in production
2. **Set up proper logging** with log rotation
3. **Monitor API usage** to avoid rate limits
4. **Implement retry logic** for transient failures (already included)
5. **Use health checks** for monitoring
6. **Keep dependencies updated** regularly
7. **Test thoroughly** before deploying changes

---

**Made with ❤️ for production use**