import json

from models.model import AppModelConfig, App
from core.moderation.factory import ModerationFactory
from extensions.ext_database import db

class ModerationService:

    def moderation_for_outputs(self, app_model: App, text: str) -> dict:
        app_model_config: AppModelConfig = None

        app_model_config = db.session.query(AppModelConfig).filter(AppModelConfig.id == app_model.app_model_config_id).first()

        if not app_model_config:
            raise ValueError("app model config not found")
        
        name = app_model_config.sensitive_word_avoidance_dict['type']
        config = app_model_config.sensitive_word_avoidance_dict['config']

        moderation = ModerationFactory(name, app_model.tenant_id, config)
        data =  moderation.moderation_for_outputs(text).json()
        return json.loads(data)