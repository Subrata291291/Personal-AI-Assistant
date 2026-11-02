# Personal AI Assistant Platform

A multi-user AI-powered personal assistant.

## Technology Stack

### Backend
- Python 3.12
- FastAPI
- SQLAlchemy
- Alembic
- Pydantic

### Frontend
- React
- Vite
- JavaScript

### Database and Authentication
- Supabase Auth
- Supabase PostgreSQL
- PostgreSQL Row Level Security
- pgvector

### AI Architecture
- Groq
- OpenRouter
- Google Gemini
- OpenAI
- LangGraph
- MCP Python SDK

## LLM Fallback Priority

Groq -> OpenRouter -> Gemini -> OpenAI

Groq will be attempted only once per request.

## Memory Architecture

1. Short-term memory
2. Long-term memory
3. Episodic memory
4. Semantic memory

## Deployment

- Backend: Render
- Frontend: Netlify
- Database: Existing Supabase
- Source Control: GitHub

## Security

- Never commit .env
- Never expose API keys
- Enforce user-level data isolation
- Use PostgreSQL RLS
- Encrypt Google OAuth refresh tokens
- Require approval for consequential operations

## Development Rules

- No local database
- No Docker
- No automated test files
- Incremental database migrations
- Live deployment verification

## Current Progress

Phase 0: Project Foundation
