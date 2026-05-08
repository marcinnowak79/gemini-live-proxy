#!/usr/bin/with-contenv bashio

bashio::log.info "Starting Gemini Live Proxy..."

export GEMINI_API_KEY=$(bashio::config 'gemini_api_key')
export GEMINI_MODEL=$(bashio::config 'gemini_model')
export GEMINI_VOICE=$(bashio::config 'gemini_voice')
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
export TIMER_MEDIA_PLAYER_ENTITY_ID=$(bashio::config 'timer_media_player_entity_id')
CAPTURE_ENABLED_CONFIG=$(bashio::config 'capture_enabled')
case "${CAPTURE_ENABLED_CONFIG,,}" in
    "1"|"true"|"yes"|"on")
        export CAPTURE_ENABLED="true"
        ;;
    *)
        export CAPTURE_ENABLED="false"
        ;;
esac
export CAPTURE_DIR=$(bashio::config 'capture_dir')
export CAPTURE_MAX_SECONDS=$(bashio::config 'capture_max_seconds')
export TIMER_DEFAULT_MEDIA_URL=$(bashio::config 'timer_default_media_url')
export TIMER_DEFAULT_MEDIA_CONTENT_TYPE=$(bashio::config 'timer_default_media_content_type')
export TIMER_DEFAULT_SCRIPT_ID=$(bashio::config 'timer_default_script_id')
export TIMER_ALARM_REPEAT_INTERVAL_SECONDS=$(bashio::config 'timer_alarm_repeat_interval_seconds')
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

bashio::log.info "Model: ${GEMINI_MODEL}, Voice: ${GEMINI_VOICE}"
bashio::log.info "Debug logging: ${DEBUG_LOGGING}"
bashio::log.info "Capture mode: ${CAPTURE_ENABLED}, dir: ${CAPTURE_DIR}"
bashio::log.info "Capture max seconds: ${CAPTURE_MAX_SECONDS:-2.0}"

cd /app
if nice -n -10 true 2>/dev/null; then
    bashio::log.info "Starting proxy with elevated scheduler priority (nice -10)"
    exec nice -n -10 python3 -u proxy_server.py
fi

bashio::log.warning "Could not raise scheduler priority; starting proxy with default priority"
exec python3 -u proxy_server.py
