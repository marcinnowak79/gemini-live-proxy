#!/usr/bin/with-contenv bashio

bashio::log.info "Starting Gemini Live Proxy..."

export AI_PROVIDER=$(bashio::config 'ai_provider')
export AI_PROVIDER_ENTITY=$(bashio::config 'ai_provider_entity')
export GEMINI_API_KEY=$(bashio::config 'gemini_api_key')
export GEMINI_MODEL=$(bashio::config 'gemini_model')
export GEMINI_VOICE=$(bashio::config 'gemini_voice')
export OPENAI_API_KEY=$(bashio::config 'openai_api_key')
export OPENAI_MODEL=$(bashio::config 'openai_model')
export OPENAI_VOICE=$(bashio::config 'openai_voice')
export ASSISTANT_NAME=$(bashio::config 'assistant_name')
export ASSISTANT_GENDER=$(bashio::config 'assistant_gender')
export ASSISTANT_SPEAKING_STYLE=$(bashio::config 'assistant_speaking_style')
export ASSISTANT_LANGUAGE=$(bashio::config 'assistant_language')
export ASSISTANT_RESPONSE_LANGUAGE=$(bashio::config 'assistant_response_language')
export SYSTEM_PROMPT_TEMPLATE=$(bashio::config 'system_prompt_template')
export ROOM_ALIASES_JSON=$(bashio::config 'room_aliases_json')
export VACUUM_ENTITY_ID=$(bashio::config 'vacuum_entity_id')
export HA_EXPOSED_ONLY=$(bashio::config 'ha_exposed_only')
DEBUG_LOGGING_CONFIG=$(bashio::config 'debug_logging')
case "${DEBUG_LOGGING_CONFIG,,}" in
    "1"|"true"|"yes"|"on")
        export DEBUG_LOGGING="true"
        ;;
    *)
        export DEBUG_LOGGING="false"
        ;;
esac
SAVE_INPUT_AUDIO_CONFIG=$(bashio::config 'save_input_audio')
case "${SAVE_INPUT_AUDIO_CONFIG,,}" in
    "1"|"true"|"yes"|"on")
        export SAVE_INPUT_AUDIO="true"
        ;;
    *)
        export SAVE_INPUT_AUDIO="false"
        ;;
esac
export TIMER_MEDIA_PLAYER_ENTITY_ID=$(bashio::config 'timer_media_player_entity_id')
export RESPONSE_PREBUFFER_MS=$(bashio::config 'response_prebuffer_ms')
export SESSION_TIMEOUT_SECONDS=$(bashio::config 'session_timeout_seconds')
export GEMINI_MAX_RETRIES=$(bashio::config 'gemini_max_retries')
export TIMER_DEFAULT_MEDIA_URL=$(bashio::config 'timer_default_media_url')
export TIMER_DEFAULT_MEDIA_CONTENT_TYPE=$(bashio::config 'timer_default_media_content_type')
export TIMER_DEFAULT_SCRIPT_ID=$(bashio::config 'timer_default_script_id')
export TIMER_ALARM_REPEAT_INTERVAL_SECONDS=$(bashio::config 'timer_alarm_repeat_interval_seconds')
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

bashio::log.info "Model: ${GEMINI_MODEL}, Voice: ${GEMINI_VOICE}"
bashio::log.info "Debug logging: ${DEBUG_LOGGING}"

cd /app
if nice -n -10 true 2>/dev/null; then
    bashio::log.info "Starting proxy with elevated scheduler priority (nice -10)"
    exec nice -n -10 python3 -u proxy_server.py
fi

bashio::log.warning "Could not raise scheduler priority; starting proxy with default priority"
exec python3 -u proxy_server.py
