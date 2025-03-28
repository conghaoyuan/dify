import decimal
import json
from typing import Optional, Union

from core.callback_handler.entity.agent_loop import AgentLoop
from core.callback_handler.entity.dataset_query import DatasetQueryObj
from core.callback_handler.entity.llm_message import LLMMessage
from core.callback_handler.entity.chain_result import ChainResult
from core.model_providers.model_factory import ModelFactory
from core.model_providers.models.entity.message import to_prompt_messages, MessageType
from core.model_providers.models.llm.base import BaseLLM
from core.prompt.prompt_builder import PromptBuilder
from core.prompt.prompt_template import JinjaPromptTemplate
from events.message_event import message_was_created
from extensions.ext_database import db
from extensions.ext_redis import redis_client
from models.dataset import DatasetQuery
from models.model import AppModelConfig, Conversation, Account, Message, EndUser, App, MessageAgentThought, MessageChain


class ConversationMessageTask:
    def __init__(self, task_id: str, app: App, app_model_config: AppModelConfig, user: Account,
                 inputs: dict, query: str, streaming: bool, model_instance: BaseLLM,
                 conversation: Optional[Conversation] = None, is_override: bool = False):
        self.task_id = task_id

        self.app = app
        self.tenant_id = app.tenant_id
        self.app_model_config = app_model_config
        self.is_override = is_override

        self.user = user
        self.inputs = inputs
        self.query = query
        self.streaming = streaming

        self.conversation = conversation
        self.is_new_conversation = False

        self.model_instance = model_instance

        self.message = None

        self.model_dict = self.app_model_config.model_dict
        self.provider_name = self.model_dict.get('provider')
        self.model_name = self.model_dict.get('name')
        self.mode = app.mode

        self.init()

        self._pub_handler = PubHandler(
            user=self.user,
            task_id=self.task_id,
            message=self.message,
            conversation=self.conversation,
            chain_pub=False,  # disabled currently
            agent_thought_pub=True
        )

    def init(self):
        override_model_configs = None
        if self.is_override:
            override_model_configs = {
                "model": self.app_model_config.model_dict,
                "pre_prompt": self.app_model_config.pre_prompt,
                "agent_mode": self.app_model_config.agent_mode_dict,
                "opening_statement": self.app_model_config.opening_statement,
                "suggested_questions": self.app_model_config.suggested_questions_list,
                "suggested_questions_after_answer": self.app_model_config.suggested_questions_after_answer_dict,
                "more_like_this": self.app_model_config.more_like_this_dict,
                "sensitive_word_avoidance": self.app_model_config.sensitive_word_avoidance_dict,
                "user_input_form": self.app_model_config.user_input_form_list,
            }

        introduction = ''
        system_instruction = ''
        system_instruction_tokens = 0
        if self.mode == 'chat':
            introduction = self.app_model_config.opening_statement
            if introduction:
                prompt_template = JinjaPromptTemplate.from_template(template=introduction)
                prompt_inputs = {k: self.inputs[k] for k in prompt_template.input_variables if k in self.inputs}
                try:
                    introduction = prompt_template.format(**prompt_inputs)
                except KeyError:
                    pass

            if self.app_model_config.pre_prompt:
                system_message = PromptBuilder.to_system_message(self.app_model_config.pre_prompt, self.inputs)
                system_instruction = system_message.content
                model_instance = ModelFactory.get_text_generation_model(
                    tenant_id=self.tenant_id,
                    model_provider_name=self.provider_name,
                    model_name=self.model_name
                )
                system_instruction_tokens = model_instance.get_num_tokens(to_prompt_messages([system_message]))

        if not self.conversation:
            self.is_new_conversation = True
            self.conversation = Conversation(
                app_id=self.app_model_config.app_id,
                app_model_config_id=self.app_model_config.id,
                model_provider=self.provider_name,
                model_id=self.model_name,
                override_model_configs=json.dumps(override_model_configs) if override_model_configs else None,
                mode=self.mode,
                name='',
                inputs=self.inputs,
                introduction=introduction,
                system_instruction=system_instruction,
                system_instruction_tokens=system_instruction_tokens,
                status='normal',
                from_source=('console' if isinstance(self.user, Account) else 'api'),
                from_end_user_id=(self.user.id if isinstance(self.user, EndUser) else None),
                from_account_id=(self.user.id if isinstance(self.user, Account) else None),
            )

            db.session.add(self.conversation)
            db.session.flush()

        self.message = Message(
            app_id=self.app_model_config.app_id,
            model_provider=self.provider_name,
            model_id=self.model_name,
            override_model_configs=json.dumps(override_model_configs) if override_model_configs else None,
            conversation_id=self.conversation.id,
            inputs=self.inputs,
            query=self.query,
            message="",
            message_tokens=0,
            message_unit_price=0,
            answer="",
            answer_tokens=0,
            answer_unit_price=0,
            provider_response_latency=0,
            total_price=0,
            currency=self.model_instance.get_currency(),
            from_source=('console' if isinstance(self.user, Account) else 'api'),
            from_end_user_id=(self.user.id if isinstance(self.user, EndUser) else None),
            from_account_id=(self.user.id if isinstance(self.user, Account) else None),
            agent_based=self.app_model_config.agent_mode_dict.get('enabled'),
        )

        db.session.add(self.message)
        db.session.flush()

    def append_message_text(self, text: str):
        self._pub_handler.pub_text(text)

    def save_message(self, llm_message: LLMMessage, by_stopped: bool = False):
        message_tokens = llm_message.prompt_tokens
        answer_tokens = llm_message.completion_tokens
        message_unit_price = self.model_instance.get_token_price(1, MessageType.HUMAN)
        answer_unit_price = self.model_instance.get_token_price(1, MessageType.ASSISTANT)

        total_price = self.calc_total_price(message_tokens, message_unit_price, answer_tokens, answer_unit_price)

        self.message.message = llm_message.prompt
        self.message.message_tokens = message_tokens
        self.message.message_unit_price = message_unit_price
        self.message.answer = PromptBuilder.process_template(llm_message.completion.strip()) if llm_message.completion else ''
        self.message.answer_tokens = answer_tokens
        self.message.answer_unit_price = answer_unit_price
        self.message.provider_response_latency = llm_message.latency
        self.message.total_price = total_price

        db.session.commit()

        message_was_created.send(
            self.message,
            conversation=self.conversation,
            is_first_message=self.is_new_conversation
        )

        if not by_stopped:
            self.end()

    def init_chain(self, chain_result: ChainResult):
        message_chain = MessageChain(
            message_id=self.message.id,
            type=chain_result.type,
            input=json.dumps(chain_result.prompt),
            output=''
        )

        db.session.add(message_chain)
        db.session.flush()

        return message_chain

    def on_chain_end(self, message_chain: MessageChain, chain_result: ChainResult):
        message_chain.output = json.dumps(chain_result.completion)

        self._pub_handler.pub_chain(message_chain)

    def on_agent_start(self, message_chain: MessageChain, agent_loop: AgentLoop) -> MessageAgentThought:
        message_agent_thought = MessageAgentThought(
            message_id=self.message.id,
            message_chain_id=message_chain.id,
            position=agent_loop.position,
            thought=agent_loop.thought,
            tool=agent_loop.tool_name,
            tool_input=agent_loop.tool_input,
            message=agent_loop.prompt,
            answer=agent_loop.completion,
            created_by_role=('account' if isinstance(self.user, Account) else 'end_user'),
            created_by=self.user.id
        )

        db.session.add(message_agent_thought)
        db.session.flush()

        self._pub_handler.pub_agent_thought(message_agent_thought)

        return message_agent_thought

    def on_agent_end(self, message_agent_thought: MessageAgentThought, agent_model_instant: BaseLLM,
                     agent_loop: AgentLoop):
        agent_message_unit_price = agent_model_instant.get_token_price(1, MessageType.HUMAN)
        agent_answer_unit_price = agent_model_instant.get_token_price(1, MessageType.ASSISTANT)

        loop_message_tokens = agent_loop.prompt_tokens
        loop_answer_tokens = agent_loop.completion_tokens

        loop_total_price = self.calc_total_price(
            loop_message_tokens,
            agent_message_unit_price,
            loop_answer_tokens,
            agent_answer_unit_price
        )

        message_agent_thought.observation = agent_loop.tool_output
        message_agent_thought.tool_process_data = ''  # currently not support
        message_agent_thought.message_token = loop_message_tokens
        message_agent_thought.message_unit_price = agent_message_unit_price
        message_agent_thought.answer_token = loop_answer_tokens
        message_agent_thought.answer_unit_price = agent_answer_unit_price
        message_agent_thought.latency = agent_loop.latency
        message_agent_thought.tokens = agent_loop.prompt_tokens + agent_loop.completion_tokens
        message_agent_thought.total_price = loop_total_price
        message_agent_thought.currency = agent_model_instant.get_currency()
        db.session.flush()

    def on_dataset_query_end(self, dataset_query_obj: DatasetQueryObj):
        dataset_query = DatasetQuery(
            dataset_id=dataset_query_obj.dataset_id,
            content=dataset_query_obj.query,
            source='app',
            source_app_id=self.app.id,
            created_by_role=('account' if isinstance(self.user, Account) else 'end_user'),
            created_by=self.user.id
        )

        db.session.add(dataset_query)

    def calc_total_price(self, message_tokens, message_unit_price, answer_tokens, answer_unit_price):
        message_tokens_per_1k = (decimal.Decimal(message_tokens) / 1000).quantize(decimal.Decimal('0.001'),
                                                                                  rounding=decimal.ROUND_HALF_UP)
        answer_tokens_per_1k = (decimal.Decimal(answer_tokens) / 1000).quantize(decimal.Decimal('0.001'),
                                                                                rounding=decimal.ROUND_HALF_UP)

        total_price = message_tokens_per_1k * message_unit_price + answer_tokens_per_1k * answer_unit_price
        return total_price.quantize(decimal.Decimal('0.0000001'), rounding=decimal.ROUND_HALF_UP)

    def end(self):
        self._pub_handler.pub_end()


class PubHandler:
    def __init__(self, user: Union[Account | EndUser], task_id: str,
                 message: Message, conversation: Conversation,
                 chain_pub: bool = False, agent_thought_pub: bool = False):
        self._channel = PubHandler.generate_channel_name(user, task_id)
        self._stopped_cache_key = PubHandler.generate_stopped_cache_key(user, task_id)

        self._task_id = task_id
        self._message = message
        self._conversation = conversation
        self._chain_pub = chain_pub
        self._agent_thought_pub = agent_thought_pub

    @classmethod
    def generate_channel_name(cls, user: Union[Account | EndUser], task_id: str):
        if not user:
            raise ValueError("user is required")

        user_str = 'account-' + str(user.id) if isinstance(user, Account) else 'end-user-' + str(user.id)
        return "generate_result:{}-{}".format(user_str, task_id)

    @classmethod
    def generate_stopped_cache_key(cls, user: Union[Account | EndUser], task_id: str):
        user_str = 'account-' + str(user.id) if isinstance(user, Account) else 'end-user-' + str(user.id)
        return "generate_result_stopped:{}-{}".format(user_str, task_id)

    def pub_text(self, text: str):
        content = {
            'event': 'message',
            'data': {
                'task_id': self._task_id,
                'message_id': str(self._message.id),
                'text': text,
                'mode': self._conversation.mode,
                'conversation_id': str(self._conversation.id)
            }
        }

        redis_client.publish(self._channel, json.dumps(content))

        if self._is_stopped():
            self.pub_end()
            raise ConversationTaskStoppedException()

    def pub_chain(self, message_chain: MessageChain):
        if self._chain_pub:
            content = {
                'event': 'chain',
                'data': {
                    'task_id': self._task_id,
                    'message_id': self._message.id,
                    'chain_id': message_chain.id,
                    'type': message_chain.type,
                    'input': json.loads(message_chain.input),
                    'output': json.loads(message_chain.output),
                    'mode': self._conversation.mode,
                    'conversation_id': self._conversation.id
                }
            }

            redis_client.publish(self._channel, json.dumps(content))

        if self._is_stopped():
            self.pub_end()
            raise ConversationTaskStoppedException()

    def pub_agent_thought(self, message_agent_thought: MessageAgentThought):
        if self._agent_thought_pub:
            content = {
                'event': 'agent_thought',
                'data': {
                    'id': message_agent_thought.id,
                    'task_id': self._task_id,
                    'message_id': self._message.id,
                    'chain_id': message_agent_thought.message_chain_id,
                    'position': message_agent_thought.position,
                    'thought': message_agent_thought.thought,
                    'tool': message_agent_thought.tool,
                    'tool_input': message_agent_thought.tool_input,
                    'mode': self._conversation.mode,
                    'conversation_id': self._conversation.id
                }
            }

            redis_client.publish(self._channel, json.dumps(content))

        if self._is_stopped():
            self.pub_end()
            raise ConversationTaskStoppedException()


    def pub_end(self):
        content = {
            'event': 'end',
        }

        redis_client.publish(self._channel, json.dumps(content))

    @classmethod
    def pub_error(cls, user: Union[Account | EndUser], task_id: str, e):
        content = {
            'error': type(e).__name__,
            'description': e.description if getattr(e, 'description', None) is not None else str(e)
        }

        channel = cls.generate_channel_name(user, task_id)
        redis_client.publish(channel, json.dumps(content))

    def _is_stopped(self):
        return redis_client.get(self._stopped_cache_key) is not None

    @classmethod
    def ping(cls, user: Union[Account | EndUser], task_id: str):
        content = {
            'event': 'ping'
        }

        channel = cls.generate_channel_name(user, task_id)
        redis_client.publish(channel, json.dumps(content))

    @classmethod
    def stop(cls, user: Union[Account | EndUser], task_id: str):
        stopped_cache_key = cls.generate_stopped_cache_key(user, task_id)
        redis_client.setex(stopped_cache_key, 600, 1)


class ConversationTaskStoppedException(Exception):
    pass
