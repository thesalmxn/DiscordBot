# Herbs Are My World - Discord Bot

A feature-rich Discord bot with streaming monitoring, task management, workflow automation, Trello integration, and AI-powered assistance. This bot is designed to help manage community interactions, track workflows, and automate routine management tasks.

## Features

### 🎬 Streaming Monitor
- Track user streaming sessions with automatic logging
- Log streaming starts and ends in dedicated Discord channels
- Maintain persistent streaming data across bot restarts
- Timezone-aware timestamp tracking

### 📋 Task Management
- Create, update, and delete tasks with `!task` commands
- Get hourly work reminders for active tasks
- Track tasks with IDs, priorities, and descriptions
- Persistent task storage with JSON serialization

### 🔄 Workflow Automation
- Create visual workflow diagrams on Miro boards
- Support for multiple diagram shapes: start, end, process, decision nodes
- AI-powered workflow optimization using Ollama
- **Each workflow gets its own dedicated Miro board** (no overlap)
- Display workflow diagrams as embeds in Discord

### 🎯 Trello Integration
- Map and sync tasks to Trello boards
- Automatic board synchronization
- Miro-to-Trello board mapping support

### 👥 Check-in System
- Automated periodic check-ins with community members via DM
- Track responses in CSV format
- Customizable check-in intervals

### ⏰ Break Timer System
- Start and monitor work break timers for users
- End-of-day WhatsApp reminder summaries at configurable times
- Role-based reminder targeting

### 🤖 Ollama AI Integration
- Generate workflow suggestions and optimizations
- AI-powered text generation for workflow descriptions
- Customizable LLM model selection

## Project Structure

```
.
├── bot.py          # Main bot with full features (streaming, tasks, workflows)
├── streaming_monitor.py             # Standalone streaming monitor module
├── workflow_manager.py              # Standalone workflow manager and Miro integration
├── requirements.txt                 # Python dependencies
├── Dockerfile                       # Docker container setup
├── docker-compose.yml              # Docker compose configuration
├── README.md                        # This file
└── data/
    ├── checkins.csv                 # Check-in response logs
    ├── miro_map.json               # Miro board to Discord mapping
    ├── streaming.json              # Streaming session history
    ├── tasks.json                  # Task database
    ├── trello_map.json             # Trello board mapping
    └── workflows.json              # Workflow definitions
```

## Setup & Installation

### Prerequisites
- Python 3.11 or higher
- Discord bot token with proper intents enabled
- (Optional) Miro API access token for workflow visualization
- (Optional) Trello API access token
- (Optional) Ollama server for AI features

### Step 1: Clone or Download
```bash
git clone <repository-url>
cd DiscordBot
```

### Step 2: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 3: Create Environment Variables File
Create a `.env` file in the project root. For reference, copy from `env_DimDiscord` template or use the variables below:

```env
# Discord Bot (Required)
DISCORD_TOKEN=your_discord_bot_token_here

# Data Directory (Optional)
DATA_DIR=/app/data          # For Docker: /app/data, local: ./data

# Timezone (Optional)
TIMEZONE=Asia/Nicosia       # Adjust to your timezone

# Streaming Logs (Optional)
STREAMING_LOG_CHANNEL=streaming-logs

# Ollama AI Integration (Optional)
OLLAMA_URL=http://192.168.10.7:11434/api/generate
OLLAMA_MODEL=llama3

# Miro Integration (Optional)
MIRO_ACCESS_TOKEN=your_miro_token
MIRO_BOARD_ID=your_board_id

# Trello Integration (Optional)
TRELLO_API_KEY=your_trello_key
TRELLO_TOKEN=your_trello_token
```

**Note:** Never commit the `.env` file to version control. Add it to `.gitignore` if not already present.

### Step 4: Run the Bot

**Locally:**
```bash
python bot.py
```

**With Docker:**
```bash
docker-compose up --build
```

## Bot Commands

### Task Management
- `!task list` - Show all active tasks
- `!task <description>` - Create a new task
- `!task delete <id>` - Delete a task
- `!task priority <id> <level>` - Set task priority

### Workflow Management
- `!workflow create <name>` - Create a new workflow
- `!workflow add <workflow_id> <type> <description>` - Add node to workflow
- `!workflow visualize <id>` - Display workflow diagram
- `!workflow optimize <id>` - Get AI-powered optimization suggestions

### Streaming
- Automatically tracked when users go live
- Logged in configured channel with timestamps

### Configuration
- `!setnotify <channel_id>` - Set channel for work reminders
- `!settimezone <timezone>` - Update bot timezone

### Check-ins
- Runs automatically at configured intervals
- Users respond to DMs with their status

## Environment Variables Reference

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DISCORD_TOKEN` | Yes | - | Discord bot authentication token |
| `DATA_DIR` | No | `/app/data` | Directory for persistent data storage |
| `TIMEZONE` | No | `Asia/Nicosia` | Timezone for timestamps and reminders |
| `STREAMING_LOG_CHANNEL` | No | `streaming-logs` | Channel name for streaming notifications |
| `OLLAMA_URL` | No | `http://192.168.10.7:11434/api/generate` | Ollama API endpoint for AI features |
| `OLLAMA_MODEL` | No | `llama3` | LLM model name to use |
| `MIRO_ACCESS_TOKEN` | No | - | Miro API authentication token |
| `MIRO_BOARD_ID` | No | - | Miro board ID for workflow visualization |
| `TRELLO_API_KEY` | No | - | Trello API key |
| `TRELLO_TOKEN` | No | - | Trello authentication token |

## File Formats

### tasks.json
```json
{
  "1": {
    "id": 1,
    "title": "Task Title",
    "description": "Task description",
    "priority": "high",
    "created": "2024-04-18T10:30:00",
    "status": "active"
  }
}
```

### workflows.json
```json
{
  "1": {
    "id": 1,
    "name": "Workflow Name",
    "nodes": [
      {
        "id": "node_1",
        "type": "start",
        "description": "Start point"
      }
    ],
    "created": "2024-04-18T10:30:00"
  }
}
```

### streaming.json
```json
{
  "user_id_123": {
    "username": "username",
    "sessions": [
      {
        "start": "2024-04-18T14:00:00",
        "end": "2024-04-18T16:30:00"
      }
    ],
    "current_start": null
  }
}
```

## Docker Deployment

The project includes a `Dockerfile` and `docker-compose.yml` for containerized deployment.

### Build
```bash
docker-compose build
```

### Run
```bash
docker-compose up -d
```

### View Logs
```bash
docker-compose logs -f
```

### Stop
```bash
docker-compose down
```

The Docker setup includes:
- Python 3.11-slim base image
- Volume mounts for persistent data
- Environment variable support
- Unbuffered output for real-time logging

## Troubleshooting

### Miro Integration Issues

**Problem**: Workflows create successfully but Miro diagrams show shapes without connectors.

**Solution**: Ensure Miro API position values use percentage strings ("50%") instead of numeric values (0.5). The connector position format was updated in workflow_manager.py to fix this API requirement.

**Problem**: "Miro not configured" error when workflows should work.

**Solution**: Ensure the `.env` file is loaded with `load_dotenv()` before accessing Miro environment variables. The workflow manager checks for `MIRO_ACCESS_TOKEN` and `MIRO_BOARD_ID` to enable Miro features.

**Problem**: AI workflow generation fails with JSON parsing errors.

**Solution**: The validation function now handles string indices from AI responses by converting them to integers. This prevents TypeError when comparing string indices to integer bounds.

**Problem**: Workflows overlap on the same Miro board.

**Solution**: Each workflow now creates its own dedicated Miro board automatically. No configuration changes needed - this is the new default behavior.

### General Debugging

To test individual components:

```bash
# Test workflow creation
python -c "
from dotenv import load_dotenv
load_dotenv()
import asyncio
import workflow_manager

async def test():
    async def send(msg): print('MSG:', msg)
    await workflow_manager.cmd_workflow_create(send, 'test workflow', 'user')

asyncio.run(test())
"

# Check Miro configuration
python -c "
from dotenv import load_dotenv
load_dotenv()
import os
print('MIRO_ACCESS_TOKEN:', bool(os.getenv('MIRO_ACCESS_TOKEN')))
print('MIRO_BOARD_ID:', bool(os.getenv('MIRO_BOARD_ID')))
"
```

### Updating Dependencies

After installing new packages, update requirements.txt:

```bash
pip freeze > requirements.txt
```

## Modu bot with Trello integration capabilities for board management and synchronization.

### bot.py ⭐ **Main Implementation**
Full-featured bot combining:
- Streaming monitoring
- Task management
- Workflow automation with Miro
- Trello integration
- Check-in system
- Break timer management

### streaming_monitor.py
Standalone module for:
- Tracking user streaming sessions
- Logging streaming events to Discord
- Maintaining streaming history
- Exporting session data

### workflow_manager.py
Standalone module for:
- Creating and managing workflows
- Miro board integration
- Workflow visualization
- AI-powered optimization
- Diagram generation with shapes and colors

## Logging

Logs are stored in `data/discord.log` and displayed in console with timestamps and log levels (DEBUG, INFO, WARNING, ERROR).

Log format:
```
YYYY-MM-DD HH:MM:SS [LEVEL] Message
```

## Troubleshooting

### Bot doesn't respond
- Verify `DISCORD_TOKEN` is correct in `.env`
- Check that bot has appropriate Discord intents enabled
- Ensure bot has permissions in the target server/channel

### Streaming not detected
- Verify `SERVER MEMBERS INTENT` is enabled in Discord Developer Portal
- Check `intents.members = True` in bot configuration

### Timezone issues
- Install `tzdata` package: `pip install tzdata`
- Check `TIMEZONE` environment variable
- Verify your system timezone is set correctly

### Miro integration not working
- Verify `MIRO_ACCESS_TOKEN` and `MIRO_BOARD_ID` are set
- Check Miro API credentials in Discord Developer settings
- Ensure Miro access token hasn't expired

### Ollama AI features disabled
- If `OLLAMA_URL` is not set, AI features are gracefully disabled
- Start Ollama server before running bot for AI features
- Verify `OLLAMA_MODEL` matches installed model

## Development

### Adding New Commands
Edit the main bot file and use `@bot.command()` decorator:
```python
@bot.command()
async def mycommand(ctx, arg):
    """Command description"""
    await ctx.send("Response")
```

### Adding New Modules
Create a new `.py` file and import it in the main bot file:
```python
import my_new_module
```

### Testing
```bash
python -m pytest tests/
```

## License

[Add your license here]

## Support

For issues, questions, or contributions, please open an issue or contact the project maintainers.

## Version History

- **v1.0.0** - Initial release with streaming, tasks, workflows, and Trello integration
