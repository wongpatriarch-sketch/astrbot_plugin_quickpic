"""快捷图库插件。

监听全部消息，用消息纯文本作为关键词去持久化图片目录里找同名图片；
命中则只回一张纯图片并终止事件传播，未命中则静默放行给后续流程。
"""

from collections.abc import AsyncGenerator
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import EventMessageType, event_message_type
from astrbot.api.star import Context, Star, StarTools

PLUGIN_NAME = "astrbot_plugin_quickpic"

# 支持的图片扩展名，元组顺序即同名多后缀时的取用优先级
IMAGE_EXTENSIONS: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
)

# 关键词中出现这些片段一律视为路径穿越尝试
PATH_ESCAPE_TOKENS: tuple[str, ...] = ("/", "\\", "..", "\x00")


class QuickPic(Star):
    """按指令关键词回复 data/plugin_data 下的同名本地图片。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.image_dir: Path | None = self._prepare_image_dir()

    def _prepare_image_dir(self) -> Path | None:
        """定位并创建图片目录，失败时返回 None 让插件保持静默而不抛错。"""
        try:
            image_dir = StarTools.get_data_dir(PLUGIN_NAME) / "images"
            image_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError) as exc:
            logger.error(f"[快捷图库] 图片目录创建失败，插件将静默不响应：{exc}")
            return None

        logger.info(f"[快捷图库] 请把图片放到该目录：{image_dir}")
        return image_dir

    @staticmethod
    def _extract_keyword(message_str: str) -> str | None:
        """从消息纯文本中提取关键词，为空或含路径穿越字符时返回 None。"""
        keyword = message_str.strip().removeprefix("/").strip()
        if not keyword or any(token in keyword for token in PATH_ESCAPE_TOKENS):
            return None
        return keyword

    def _find_image(self, keyword: str) -> Path | None:
        """每次调用都实时扫描目录，因此新增或替换图片无需重载插件。"""
        if self.image_dir is None:
            return None

        target = keyword.casefold()
        try:
            matched = [
                entry
                for entry in self.image_dir.iterdir()
                if entry.stem.casefold() == target
                and entry.suffix.lower() in IMAGE_EXTENSIONS
                and entry.is_file()
            ]
        except OSError as exc:
            logger.error(f"[快捷图库] 读取图片目录失败：{exc}")
            return None

        if not matched:
            return None

        # 文件名也参与排序：Linux 上 Menu.png 与 menu.png 可共存，
        # 不加这一层的话命中哪张会取决于 readdir 顺序。
        matched.sort(
            key=lambda entry: (
                IMAGE_EXTENSIONS.index(entry.suffix.lower()),
                entry.name,
            ),
        )
        return matched[0]

    @event_message_type(EventMessageType.ALL)
    async def on_message(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        """全局消息监听器：命中图片则只发图片并阻断事件。"""
        keyword = self._extract_keyword(event.message_str)
        if keyword is None:
            return

        image_path = self._find_image(keyword)
        if image_path is None:
            # 未命中：不提示、不阻断，让消息继续流向其他插件或 LLM
            return

        logger.info(f"[快捷图库] 关键词「{keyword}」命中 {image_path.name}")
        yield event.image_result(str(image_path))
        # 顺序不能颠倒：先 stop_event() 会让调度器在本次 yield 后直接跳出管道，
        # ResultDecorate/Respond 阶段不再执行，图片就发不出去了。
        event.stop_event()
