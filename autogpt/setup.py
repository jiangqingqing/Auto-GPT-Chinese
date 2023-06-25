"""Set up the AI and its goals"""
import re

from colorama import Fore, Style
from jinja2 import Template

from autogpt import utils
from autogpt.config import Config
from autogpt.config.ai_config import AIConfig
from autogpt.llm.base import ChatSequence, Message
from autogpt.llm.chat import create_chat_completion
from autogpt.logs import logger
from autogpt.prompts.default_prompts import (
    DEFAULT_SYSTEM_PROMPT_AICONFIG_AUTOMATIC,
    DEFAULT_TASK_PROMPT_AICONFIG_AUTOMATIC,
    DEFAULT_USER_DESIRE_PROMPT,
)


def prompt_user(config: Config) -> AIConfig:
    """Prompt the user for input

    Returns:
        AIConfig: The AIConfig object tailored to the user's input
    """
    ai_name = ""
    ai_config = None

    # Construct the prompt
    logger.typewriter_log(
        "欢迎来到Auto-GPT! ",
        Fore.GREEN,
        "执行 '--help' 获取更多信息.",
        speak_text=True,
    )

    # Get user desire
    logger.typewriter_log(
        "创建一个AI助手:",
        Fore.GREEN,
        "输入 '--manual' 进入手动模式.",
        speak_text=True,
    )

    user_desire = utils.clean_input(
        config, f"{Fore.LIGHTBLUE_EX}我希望Auto-GPT帮我{Style.RESET_ALL}: "
    )

    if user_desire == "":
        user_desire = DEFAULT_USER_DESIRE_PROMPT  # Default prompt

    # If user desire contains "--manual"
    if "--manual" in user_desire:
        logger.typewriter_log(
            "手动模式已启动",
            Fore.GREEN,
            speak_text=True,
        )
        return generate_aiconfig_manual(config)

    else:
        try:
            return generate_aiconfig_automatic(user_desire, config)
        except Exception as e:
            logger.typewriter_log(
                "无法基于用户偏好生成AI配置.",
                Fore.RED,
                "回滚至手动模式.",
                speak_text=True,
            )

            return generate_aiconfig_manual(config)


def generate_aiconfig_manual(config: Config) -> AIConfig:
    """
    Interactively create an AI configuration by prompting the user to provide the name, role, and goals of the AI.

    This function guides the user through a series of prompts to collect the necessary information to create
    an AIConfig object. The user will be asked to provide a name and role for the AI, as well as up to five
    goals. If the user does not provide a value for any of the fields, default values will be used.

    Returns:
        AIConfig: An AIConfig object containing the user-defined or default AI name, role, and goals.
    """

    # Manual Setup Intro
    logger.typewriter_log(
        "建立一个AI助手:",
        Fore.GREEN,
        "给你AI助手起一个名字和赋予它一个角色，什么都不输入将使用默认值.",
        speak_text=True,
    )

    # Get AI Name from User
    logger.typewriter_log(
        "你AI的名字叫: ", Fore.GREEN, "例如, '企业家-GPT'"
    )
    ai_name = utils.clean_input(config, "AI 名字: ")
    if ai_name == "":
        ai_name = "企业家-GPT"

    logger.typewriter_log(
        f"{ai_name} 在这儿呢!", Fore.LIGHTBLUE_EX, "我听从您的吩咐.", speak_text=True
    )

    # Get AI Role from User
    logger.typewriter_log(
        "描述你AI的角色: ",
        Fore.GREEN,
        "例如, '一个自动帮助你策划与经营业务的人工智能帮手，目标专注于提升你的净资产.'",
    )
    ai_role = utils.clean_input(config, f"{ai_name} is: ")
    if ai_role == "":
        ai_role = "一个自动帮助你策划与经营业务的人工智能帮手，目标专注于提升你的净资产."

    # Enter up to 5 goals for the AI
    logger.typewriter_log(
        "为你的AI定义最多5个目标:  ",
        Fore.GREEN,
        "例如: \n提升净资产, 增长Twitter账户, 自动化策划与管理多条业务线'",
    )
    logger.info("Enter nothing to load defaults, enter nothing when finished.")
    ai_goals = []
    for i in range(5):
        ai_goal = utils.clean_input(
            config, f"{Fore.LIGHTBLUE_EX}Goal{Style.RESET_ALL} {i+1}: "
        )
        if ai_goal == "":
            break
        ai_goals.append(ai_goal)
    if not ai_goals:
        ai_goals = [
            "提升净资产",
            "增长Twitter账户",
            "自动化策划与管理多条业务线",
        ]

    # Get API Budget from User
    logger.typewriter_log(
        "输入你的API预算:  ",
        Fore.GREEN,
        "例如: $1.50",
    )
    logger.info("什么都不输入将让你的AI驰骋飞翔")
    api_budget_input = utils.clean_input(
        config, f"{Fore.LIGHTBLUE_EX}预算{Style.RESET_ALL}: $"
    )
    if api_budget_input == "":
        api_budget = 0.0
    else:
        try:
            api_budget = float(api_budget_input.replace("$", ""))
        except ValueError:
            logger.typewriter_log(
                "错误的预算输入. 开启吃撑飞翔模式.", Fore.RED
            )
            api_budget = 0.0

    return AIConfig(ai_name, ai_role, ai_goals, api_budget)


def generate_aiconfig_automatic(user_prompt: str, config: Config) -> AIConfig:
    """Generates an AIConfig object from the given string.

    Returns:
    AIConfig: The AIConfig object tailored to the user's input
    """

    system_prompt = DEFAULT_SYSTEM_PROMPT_AICONFIG_AUTOMATIC
    prompt_ai_config_automatic = Template(
        DEFAULT_TASK_PROMPT_AICONFIG_AUTOMATIC
    ).render(user_prompt=user_prompt)
    # Call LLM with the string as user input
    output = create_chat_completion(
        ChatSequence.for_model(
            config.fast_llm_model,
            [
                Message("system", system_prompt),
                Message("user", prompt_ai_config_automatic),
            ],
        ),
        config,
    ).content

    # Debug LLM Output
    logger.debug(f"AI Config Generator Raw Output: {output}")

    # Parse the output
    ai_name = re.search(r"Name(?:\s*):(?:\s*)(.*)", output, re.IGNORECASE).group(1)
    ai_role = (
        re.search(
            r"Description(?:\s*):(?:\s*)(.*?)(?:(?:\n)|Goals)",
            output,
            re.IGNORECASE | re.DOTALL,
        )
        .group(1)
        .strip()
    )
    ai_goals = re.findall(r"(?<=\n)-\s*(.*)", output)
    api_budget = 0.0  # TODO: parse api budget using a regular expression

    return AIConfig(ai_name, ai_role, ai_goals, api_budget)
