#!/usr/bin/with-contenv bashio

bashio::log.info "Starting Gemini Live Proxy..."

export GEMINI_API_KEY=$(bashio::config 'gemini_api_key')
export GEMINI_MODEL=$(bashio::config 'gemini_model')
export GEMINI_VOICE=$(bashio::config 'gemini_voice')
export ASSISTANT_LANGUAGE=$(bashio::config 'assistant_language')
export ASSISTANT_RESPONSE_LANGUAGE=$(bashio::config 'assistant_response_language')
export SYSTEM_PROMPT_TEMPLATE=$(bashio::config 'system_prompt_template')
export ROOM_ALIASES_JSON=$(bashio::config 'room_aliases_json')
export VACUUM_ENTITY_ID=$(bashio::config 'vacuum_entity_id')
export HA_EXPOSED_ONLY=$(bashio::config 'ha_exposed_only')
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

bashio::log.info "Model: ${GEMINI_MODEL}, Voice: ${GEMINI_VOICE}"

cd /app
exec python3 -u proxy_server.py
