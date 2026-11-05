# What worked and what we would improve in Switch

Notes from building ProcureMate on Switch for the Flip the Switch hackathon. Written to be useful, not to complain.

## What worked

- A shared room where a person and two agents see the same conversation.
- Handing work from one agent to the other with an @mention.
- Thread replies.
- Room instructions and agent instructions.
- The bundled Mattermost made setup quick.
- Permission prompts.
- Commands such as `!agents-status` and `!interrupt`.

## What we would improve

- **The wake-up reply.** We found no setting to shorten or hide the "Starting a session to handle this, one moment" reply when an agent wakes.
- **Messages with no @.** A plain message with no @ reaches no agent unless a main agent is assigned with `!assign-agent`. With follow-ups on, a message can also reach a second agent.
- **The Directory field.** It did not control where an agent ran at first, and it behaved inconsistently later.
- **Restarts.** After one laptop shutdown, Switch Console showed a server with no agents or rooms. After another, creating a room failed once with "Bridge not running". The logs show abrupt kills at shutdown, and the docs say the server does not return by itself after a reboot.
- **A lost message.** Once, a buyer message never reached either agent's log. We do not know why.
- **An early-access build.** The Windows Console is a Canary build that updates itself.
- **Buttons.** There are no buttons for agents beyond permission prompts, so the CAC step uses typed replies.
- **Documentation gap.** The docs do not say whether an agent can post while a command is running.
