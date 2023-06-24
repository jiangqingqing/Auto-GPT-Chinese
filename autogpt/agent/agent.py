import signal
import sys
from datetime import datetime

from colorama import Fore, Style

from autogpt.commands.command import CommandRegistry
from autogpt.config import Config
from autogpt.config.ai_config import AIConfig
from autogpt.json_utils.json_fix_llm import fix_json_using_multiple_techniques
from autogpt.json_utils.utilities import LLM_DEFAULT_RESPONSE_FORMAT, validate_json
from autogpt.llm.base import ChatSequence
from autogpt.llm.chat import chat_with_ai, create_chat_completion
from autogpt.llm.providers.openai import OPEN_AI_CHAT_MODELS
from autogpt.llm.utils import count_string_tokens
from autogpt.log_cycle.log_cycle import (
    FULL_MESSAGE_HISTORY_FILE_NAME,
    NEXT_ACTION_FILE_NAME,
    PROMPT_SUPERVISOR_FEEDBACK_FILE_NAME,
    SUPERVISOR_FEEDBACK_FILE_NAME,
    USER_INPUT_FILE_NAME,
    LogCycleHandler,
)
from autogpt.logs import logger, print_assistant_thoughts
from autogpt.memory.message_history import MessageHistory
from autogpt.memory.vector import VectorMemory
from autogpt.speech import say_text
from autogpt.spinner import Spinner
from autogpt.utils import clean_input
from autogpt.workspace import Workspace


class Agent:
    """Agent class for interacting with Auto-GPT.

    Attributes:
        ai_name: The name of the agent.
        memory: The memory object to use.
        next_action_count: The number of actions to execute.
        system_prompt: The system prompt is the initial prompt that defines everything
          the AI needs to know to achieve its task successfully.
        Currently, the dynamic and customizable information in the system prompt are
          ai_name, description and goals.

        triggering_prompt: The last sentence the AI will see before answering.
            For Auto-GPT, this prompt is:
            Determine exactly one command to use, and respond using the format specified
              above:
            The triggering prompt is not part of the system prompt because between the
              system prompt and the triggering
            prompt we have contextual information that can distract the AI and make it
              forget that its goal is to find the next task to achieve.
            SYSTEM PROMPT
            CONTEXTUAL INFORMATION (memory, previous conversations, anything relevant)
            TRIGGERING PROMPT

        The triggering prompt reminds the AI about its short term meta task
        (defining the next task)
    """

    def __init__(
        self,
        ai_name: str,
        memory: VectorMemory,
        next_action_count: int,
        command_registry: CommandRegistry,
        ai_config: AIConfig,
        system_prompt: str,
        triggering_prompt: str,
        workspace_directory: str,
        config: Config,
    ):
        self.ai_name = ai_name
        self.memory = memory
        self.history = MessageHistory(self)
        self.next_action_count = next_action_count
        self.command_registry = command_registry
        self.config = config
        self.ai_config = ai_config
        self.system_prompt = system_prompt
        self.triggering_prompt = triggering_prompt
        self.workspace = Workspace(workspace_directory, config.restrict_to_workspace)
        self.created_at = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.cycle_count = 0
        self.log_cycle_handler = LogCycleHandler()
        self.fast_token_limit = OPEN_AI_CHAT_MODELS.get(
            config.fast_llm_model
        ).max_tokens

    def start_interaction_loop(self):
        # Avoid circular imports
        from autogpt.app import execute_command, get_command

        # Interaction Loop
        self.cycle_count = 0
        command_name = None
        arguments = None
        user_input = ""

        # Signal handler for interrupting y -N
        def signal_handler(signum, frame):
            if self.next_action_count == 0:
                sys.exit()
            else:
                print(
                    Fore.RED
                    + "Interrupt signal received. Stopping continuous command execution."
                    + Style.RESET_ALL
                )
                self.next_action_count = 0

        signal.signal(signal.SIGINT, signal_handler)

        while True:
            # Discontinue if continuous limit is reached
            self.cycle_count += 1
            self.log_cycle_handler.log_count_within_cycle = 0
            self.log_cycle_handler.log_cycle(
                self.ai_config.ai_name,
                self.created_at,
                self.cycle_count,
                [m.raw() for m in self.history],
                FULL_MESSAGE_HISTORY_FILE_NAME,
            )
            if (
                self.config.continuous_mode
                and self.config.continuous_limit > 0
                and self.cycle_count > self.config.continuous_limit
            ):
                logger.typewriter_log(
                    "持续模式次数已到达: ",
                    Fore.YELLOW,
                    f"{self.config.continuous_limit}",
                )
                break
            # Send message to AI, get response
            with Spinner("思考中... ", plain_output=self.config.plain_output):
                assistant_reply = chat_with_ai(
                    self.config,
                    self,
                    self.system_prompt,
                    self.triggering_prompt,
                    self.fast_token_limit,
                    self.config.fast_llm_model,
                )

            assistant_reply_json = fix_json_using_multiple_techniques(assistant_reply)
            for plugin in self.config.plugins:
                if not plugin.can_handle_post_planning():
                    continue
                assistant_reply_json = plugin.post_planning(assistant_reply_json)

            # Print Assistant thoughts
            if assistant_reply_json != {}:
                validate_json(assistant_reply_json, LLM_DEFAULT_RESPONSE_FORMAT)
                # Get command name and arguments
                try:
                    print_assistant_thoughts(
                        self.ai_name, assistant_reply_json, self.config.speak_mode
                    )
                    command_name, arguments = get_command(assistant_reply_json)
                    if self.config.speak_mode:
                        say_text(f"我想要执行命令 {command_name}")

                    arguments = self._resolve_pathlike_command_args(arguments)

                except Exception as e:
                    logger.error("错误: \n", str(e))
            self.log_cycle_handler.log_cycle(
                self.ai_config.ai_name,
                self.created_at,
                self.cycle_count,
                assistant_reply_json,
                NEXT_ACTION_FILE_NAME,
            )

            # First log new-line so user can differentiate sections better in console
            logger.typewriter_log("\n")
            logger.typewriter_log(
                "NEXT ACTION: ",
                Fore.CYAN,
                f"COMMAND = {Fore.CYAN}{command_name}{Style.RESET_ALL}  "
                f"ARGUMENTS = {Fore.CYAN}{arguments}{Style.RESET_ALL}",
            )

            if not self.config.continuous_mode and self.next_action_count == 0:
                # ### GET USER AUTHORIZATION TO EXECUTE COMMAND ###
                # Get key press: Prompt the user to press enter to continue or escape
                # to exit
                self.user_input = ""
                logger.info(
                    "输入 'y' 授权执行命令, 'y -N' 执行N步持续模式, 's' 执行自我反馈命令 或"
                    "'n' 退出程序, 或直接输入反馈 "
                    f"{self.ai_name}..."
                )
                while True:
                    if self.config.chat_messages_enabled:
                        console_input = clean_input("等待你的反馈中...")
                    else:
                        console_input = clean_input(
                            Fore.MAGENTA + "输入:" + Style.RESET_ALL
                        )
                    if console_input.lower().strip() == self.config.authorise_key:
                        user_input = "生成下一个命令的JSON"
                        break
                    elif console_input.lower().strip() == "s":
                        logger.typewriter_log(
                            "-=-=-=-=-=-=-= 思考, 推理, 计划于反思将会被AI助手校验 -=-=-=-=-=-=-=",
                            Fore.GREEN,
                            "",
                        )
                        thoughts = assistant_reply_json.get("thoughts", {})
                        self_feedback_resp = self.get_self_feedback(
                            thoughts, self.config.fast_llm_model
                        )
                        logger.typewriter_log(
                            f"SELF FEEDBACK: {self_feedback_resp}",
                            Fore.YELLOW,
                            "",
                        )
                        user_input = self_feedback_resp
                        command_name = "self_feedback"
                        break
                    elif console_input.lower().strip() == "":
                        logger.warn("Invalid input format.")
                        continue
                    elif console_input.lower().startswith(
                        f"{self.config.authorise_key} -"
                    ):
                        try:
                            self.next_action_count = abs(
                                int(console_input.split(" ")[1])
                            )
                            user_input = "生成下一个命令的JSON"
                        except ValueError:
                            logger.warn(
                               "错误的输入格式. 请输入 'y -n' n 代表"
                               " 持续模式的步数."
                            )
                            continue
                        break
                    elif console_input.lower() == self.config.exit_key:
                        user_input = "EXIT"
                        break
                    else:
                        user_input = console_input
                        command_name = "人类反馈"
                        self.log_cycle_handler.log_cycle(
                            self.ai_config.ai_name,
                            self.created_at,
                            self.cycle_count,
                            user_input,
                            USER_INPUT_FILE_NAME,
                        )
                        break

                if user_input == "生成下一个命令的JSON":
                    logger.typewriter_log(
                        "-=-=-=-=-=-=-= 命令被用户批准执行 -=-=-=-=-=-=-=",
                        Fore.MAGENTA,
                        "",
                    )
                elif user_input == "EXIT" or "exit" or "退出":
                    logger.info("退出中...")
                    break
            else:
                # First log new-line so user can differentiate sections better in console
                logger.typewriter_log("\n")
                # Print authorized commands left value
                logger.typewriter_log(
                    f"{Fore.CYAN}AUTHORISED COMMANDS LEFT: {Style.RESET_ALL}{self.next_action_count}"
                )

            # Execute command
            if command_name is not None and command_name.lower().startswith("error"):
                result = f"无法执行命令: {arguments}"
            elif command_name == "人类反馈":
                result = f"人类反馈: {user_input}"
            elif command_name == "self_feedback":
                result = f"AI反馈: {user_input}"
            else:
                for plugin in self.config.plugins:
                    if not plugin.can_handle_pre_command():
                        continue
                    command_name, arguments = plugin.pre_command(
                        command_name, arguments
                    )
                command_result = execute_command(
                    self.command_registry,
                    command_name,
                    arguments,
                    agent=self,
                )
                result = f"Command {command_name} returned: " f"{command_result}"

                result_tlength = count_string_tokens(
                    str(command_result), self.config.fast_llm_model
                )
                memory_tlength = count_string_tokens(
                    str(self.history.summary_message()), self.config.fast_llm_model
                )
                if result_tlength + memory_tlength + 600 > self.fast_token_limit:
                    result = f"Failure: command {command_name} returned too much output. \
                        Do not execute this command again with the same arguments."

                for plugin in self.config.plugins:
                    if not plugin.can_handle_post_command():
                        continue
                    result = plugin.post_command(command_name, result)
                if self.next_action_count > 0:
                    self.next_action_count -= 1

            # Check if there's a result from the command append it to the message
            # history
            if result is not None:
                self.history.add("system", result, "action_result")
                logger.typewriter_log("SYSTEM: ", Fore.YELLOW, result)
            else:
                self.history.add("system", "无法执行命令", "action_result")
                logger.typewriter_log(
                    "SYSTEM: ", Fore.YELLOW, "无法执行命令"
                )

    def _resolve_pathlike_command_args(self, command_args):
        if "directory" in command_args and command_args["directory"] in {"", "/"}:
            command_args["directory"] = str(self.workspace.root)
        else:
            for pathlike in ["filename", "directory", "clone_path"]:
                if pathlike in command_args:
                    command_args[pathlike] = str(
                        self.workspace.get_path(command_args[pathlike])
                    )
        return command_args

    def get_self_feedback(self, thoughts: dict, llm_model: str) -> str:
        """Generates a feedback response based on the provided thoughts dictionary.
        This method takes in a dictionary of thoughts containing keys such as 'reasoning',
        'plan', 'thoughts', and 'criticism'. It combines these elements into a single
        feedback message and uses the create_chat_completion() function to generate a
        response based on the input message.
        Args:
            thoughts (dict): A dictionary containing thought elements like reasoning,
            plan, thoughts, and criticism.
        Returns:
            str: A feedback response generated using the provided thoughts dictionary.
        """
        ai_role = self.ai_config.ai_role



        feedback_prompt = f"下面是来自我的消息，我是一个AI助手，角色为： {ai_role}. 请评估提供的思考，推理，计划与反思. 如果这些内容可以准确的完成该角色的任务，请回复字母'Y'后面加上一个空格，然后请解释为什么是最优方案. 日过这些信息无法实现该角色的目标, 请提供一句或多句解释一下具体的问题与建议的解决方案."
        reasoning = thoughts.get("推理", "")
        plan = thoughts.get("计划", "")
        thought = thoughts.get("思考", "")
        feedback_thoughts = thought + reasoning + plan

        prompt = ChatSequence.for_model(llm_model)
        prompt.add("user", feedback_prompt + feedback_thoughts)

        self.log_cycle_handler.log_cycle(
            self.ai_config.ai_name,
            self.created_at,
            self.cycle_count,
            prompt.raw(),
            PROMPT_SUPERVISOR_FEEDBACK_FILE_NAME,
        )

        feedback = create_chat_completion(prompt)

        self.log_cycle_handler.log_cycle(
            self.ai_config.ai_name,
            self.created_at,
            self.cycle_count,
            feedback,
            SUPERVISOR_FEEDBACK_FILE_NAME,
        )
        return feedback
