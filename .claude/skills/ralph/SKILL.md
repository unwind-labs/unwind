---
name: ralph
description: "Executes tasks one by one in a loop"
when_to_use: "When you want to execute a TODO list in a loop, one task at a time"
---

# /ralph

Step 1: Decide where the TODO tasks are listed. The caller may provide a path to a file, otherwise see if a dev/TODO.md, dev/ralph.md, dev/tasks.md, TODO.md, ralph.md, tasks.md file exists in the project root. Make sure the file exists.

Step 2: Read the first TODO to execute from the file.
- If all tasks are marked done or no TODOs are found
  - Tell user all tasks are finished
- Otherwise
  - Use `/call <task>` to execute the task
  - If task was successfully completed
    - Tell the user that the task was completed and they should verify. If user is satisfied, commit changes
  - Otherwise
    - Tell the user and wait for directions

Step 3: Go back to Step 2
