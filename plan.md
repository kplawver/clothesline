# Goal

Clothesline is a cross-coding harness "super memory".

It should probably be an MCP server that provides shared context across coding agents by maintaining a history of current sessions with agents, important decisions, preferences and potentially a message bus for agents to pass messages.  

It should have vector search, probably powered by sqlite's vector plugin, and a small frontend to serve both the MCP server and for the user to see the current state of the data and maybe to browse the data or change settings.

It should also have a small LLM that can summarize sessions for long term storage (more on that in phase one).

I think the frontend is probably best done in flask, but I'm open to ideas.

# Phase One

## Basic Requirements

* It should be installable via Homebrew (we can use the existing ~/homebrew-tap repo), and registered as a brew service.
* It can download the embedding and text models on install since we can't commit files larger than 100mb.  Probably from hugging face.
* It should be as small as possible, but not smaller.
* It should allow the user to configure the address and port it listens on.

## MCP Server Basics

* It should support the 2026-07-28 version of the MCP standard.
* The MCP server shouldn't require authentication. 
* It should only listen on localhost on a relatively high port like 19004 (something no common open source project uses but that is accessible by a non-root user on most modern operating systems).
* Every MCP tool should return its skill in the tool description so we don't need to provide a separate plugin / skill for this (this might not work, which is OK, but let's try it).  It's OK for tool descriptions to mention other tools.
* Every harness gets an id when it connects - that id should be stored for every session, decision, preference, conversation, etc.  
* Every agent and sub-agent gets its own id too so we know which one a turn belongs to.  This becomes really important in Phase 2 but is also important for sessions.  

## Frontend for Users

* Users should be able to browse and search across all data, and see the current configuration.
* The design should be basic but professional.
* It should run in the same process as the MCP server.

## Session History

The raw sessions should be kept for 3 days, and then summarized and saved indefinitely.

The design is fairly simple:

* We have a small embedding model that creates the embeddings for each turn (dropping the reasoning, if it's there) in a session.
* Those get saved in the "hot" sessions table.
* Once a day, every session older than three days gets summarized by a small text model, has embeddings created, and is stored in the "cold" sessions table.
* Agents should have quick access to previous sessions and summaries when needed. Users should also be able to ask about previous sessions, like when something happened, or when they worked on this project, etc.

## Decisions and Preferences

Decisions and preferences are basically the same thing. They are things the user has asked to be remembered for the future. They should be stored in a separate vector space, but with the same embedding model so we can use wormholes to find decisions and preferences related to the current session later, and when decisions or preferences change.

* When a decision or preference is changed, the old decision should be preserved and marked superceded by a new record so we can show the path the train of thought took.
* We _could_ also save decisions made by agents as well so we can keep track of them. We can decide that later.
* We might not need wormhole vectors as we might not need to jump from one vector space to the other, so we don't need to implement them in phase one.

# Phase Two

## Message Bus

When a harness connects to the MCP server, it gets an id that's used every time it connects.  That's how we know which harness a session, decision or preference belongs to. It's also how we know which harness a message belongs too.  Now, most coding harness can run multiple agents and they all have different ways of identifying them, so each of them will need to send an agent id as the sender, and an optional recipient agent id.  Each agent will need to "register" when it comes online so they can show up in the "online" list and when they're closed, they sign off. An agent that hasn't communicated in 15 minutes should send a ping so the server knows it's still online. Any "stale" agent connection times out after a certain amount of time (an hour without a ping).

The user should be able to see conversations in the front-end and search them, and connect them to sessions.
