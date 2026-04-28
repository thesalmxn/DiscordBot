# Discord Task Bot - Commands

All commands can be used with either `!command` or `@herbsbot command` (natural language).

---

## Task Management

| Command | Usage | Description |
|---------|-------|-------------|
| `!task_add` | `!task_add <description>` | Create a new task (syncs to Trello & Miro) |
| `!task_done` | `!task_done <task_id>` | Mark task as completed (100% progress) |
| `!task_remove` | `!task_remove <task_id>` | Permanently delete a task |
| `!task_progress` | `!task_progress <task_id> <percent>` | Update task progress (0-100) |
| `!task_status` | `!task_status <task_id> <status>` | Update task status label |
| `!task_priority` | `!task_priority <task_id> <level>` | Set priority (Normal, High, Critical, Low) |
| `!task_assign` | `!task_assign <task_id> <@member>` | Assign task to team member |
| `!task_info` | `!task_info <task_id>` | Show full task details |
| `!task_table` | `!task_table` | Display all tasks in table format |

---

## Break & Wellness

| Command | Usage | Description |
|---------|-------|-------------|
| `!rest` | `!rest [minutes]` | Start timed break (default 5 min, max 30 min) |

---

## Utilities

| Command | Usage | Description |
|---------|-------|-------------|
| `!time` | `!time` | Show bot's current time and UTC |
| `!setnotify` | `!setnotify` | Set channel for hourly work reminders |
| `!test_eod` | `!test_eod` | Manually trigger end-of-day reminder |

---

## Workflow Management

| Command | Usage | Description |
|---------|-------|-------------|
| `!workflow` | `!workflow <description>` | Create AI-designed workflow on Miro |
| `!wf_edit` | `!wf_edit <id> <changes>` | Edit workflow with natural language |
| `!wf_undo` | `!wf_undo <id>` | Undo last workflow edit |
| `!wf_list` | `!wf_list` | Show all saved workflows |
| `!wf_view` | `!wf_view <id>` | Display workflow steps as text |
| `!wf_delete` | `!wf_delete <id>` | Delete workflow from Discord and Miro |
| `!wf_redraw` | `!wf_redraw <id>` | Force redraw workflow on Miro |

---

## Natural Language Examples

### Tasks (via @mention)
- `@herbsbot add a task to review comments`
- `@herbsbot mark task 2 as done`
- `@herbsbot show all tasks`
- `@herbsbot change priority of task 1 to critical`
- `@herbsbot assign task 3 to John`

### Breaks
- `@herbsbot I need a 15 minute break`

### Workflows
- `@herbsbot create a workflow for employee onboarding`
- `@herbsbot edit workflow 1 to add a background check step`

---

## Automated Features (No Command Needed)

### Hourly Work Reminders
- **Trigger:** Mon-Fri, 9 AM - 5 PM
- **Action:** Posts reminder to channel set via `!setnotify`

### Automatic User Check-ins
- **Interval:** Every 60 minutes (configurable via `CHECKIN_INTERVAL` env var)
- **Action:** DMs online members for status updates

### End-of-Day Reminder
- **Time:** 15:58 daily (Nicosia timezone)
- **Days:** Weekdays only
- **Action:** DMs members with TARGET_ROLE_IDS roles, asks for WhatsApp summary
- **Config:** `END_OF_DAY_HOUR` and `END_OF_DAY_MINUTE` in .env
