"""AstrBot 插件：禁漫下载助手（astrbot_plugin_jmcomic）

基于 jmcomic（hect0x7/JMComic-Crawler-Python，MIT）封装，
在聊天中直接下载禁漫本子/章节、查询本子详情、站内搜索、排行榜。

设计要点：
- 下载在后台异步执行，完成后自动把结果发回原会话，不阻塞其他消息；
- 支持多个车号批量下载，支持 `p章节号` 语法下载单个章节；
- 支持取消进行中的下载任务（基于 jmcomic DownloadControl）；
- 请求优先使用 jmcomic 异步 API（curl-cffi），不阻塞 AstrBot 事件循环；
- jmcomic 配置通过插件 WebUI 配置页调整，无需手写 option 文件。

创作者：小星萤
"""

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import yaml

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    import jmcomic
except Exception as e:  # pragma: no cover - 依赖未安装时给出友好提示
    jmcomic = None  # type: ignore[assignment]
    JMCOMIC_IMPORT_ERROR = e
else:
    JMCOMIC_IMPORT_ERROR = None

PLUGIN_NAME = "astrbot_plugin_jmcomic"
DATA_DIR = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
DEFAULT_DOWNLOAD_DIR = DATA_DIR / "downloads"

# images 模式单次最多直发的图片数，防止大本子刷屏被风控
MAX_IMAGES_SEND = 50

HELP_TEXT = (
    "📥 禁漫下载助手使用说明\n\n"
    "jm download 车号 [p章节] [车号...]    下载本子/章节（后台执行，完成后通知）\n"
    "jm info 车号                         查看本子详情\n"
    "jm search 关键词 [页码]              站内搜索\n"
    "jm rank [月|周|日]                   查看排行榜\n"
    "jm list                              查看进行中的下载任务\n"
    "jm cancel 车号                       取消下载任务\n"
    "jm help                              显示本帮助\n\n"
    "示例：\n"
    "  jm download 123\n"
    "  jm download 123 456 p789\n"
    "  jm info 350234\n\n"
    "配置：插件设置页可调整客户端实现（移动端/网页端）、下载目录、发送模式（zip/pdf/images/none）、并发数、代理等。\n"
    "温馨提示：珍爱 JM，请勿一次性批量下载大量本子，避免给服务器增加压力。\n\n"
    "⚠️ 免责声明：本插件仅供个人学习交流使用，请遵守所在地区法律法规，"
    "请勿将下载内容用于传播或商业用途。"
)


def _truncate(text: Any, limit: int = 200) -> str:
    """截断异常/长文本，避免单条消息过长。"""
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _first_id(content: str) -> Optional[str]:
    """从任意文本中提取第一个数字串作为车号（jmv 风格）。"""
    match = re.search(r"\d+", content or "")
    return match.group(0) if match else None


def _parse_ids(content: str) -> Tuple[List[str], List[str]]:
    """解析下载参数。
    :return: (album_ids, photo_ids)，均去重保序。
    支持：123 / 123 456 / 123 p456 / p456 / 混合文本中的数字。
    """
    album_ids: List[str] = []
    photo_ids: List[str] = []
    for token in re.split(r"[\s,，;；]+", (content or "").strip()):
        if not token:
            continue
        match = re.fullmatch(r"p(\d+)", token)
        if match:
            photo_ids.append(match.group(1))
            continue
        match = re.fullmatch(r"(\d+)", token)
        if match:
            album_ids.append(match.group(1))
            continue
        # 混合文本（如“350谁还没看过234”）提取其中的数字串
        for num in re.findall(r"\d+", token):
            album_ids.append(num)
    album_ids = list(dict.fromkeys(album_ids))
    photo_ids = list(dict.fromkeys(photo_ids))
    return album_ids, photo_ids


class JMComicPlugin(Star):
    """禁漫下载助手主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # key -> {task, control, started, desc, ids}
        self._tasks: Dict[str, Dict[str, Any]] = {}
        # 内置 HTTP 文件服务（供 NapCat 从 astrbot 容器拉取文件发送到 QQ）
        self._file_server_proc: Optional[subprocess.Popen] = None
        self._file_server_dir: Optional[str] = None
        self._file_server_port: int = 0

    # ---------- 配置 ----------

    def _build_option(self):
        """根据插件配置生成 jmcomic JmOption（公共 API create_option_by_str）。"""
        if jmcomic is None:
            raise RuntimeError(f"jmcomic 未安装或导入失败：{JMCOMIC_IMPORT_ERROR}")
        cfg = self.config

        base_dir = str((cfg.get("download_dir") or "").strip() or DEFAULT_DOWNLOAD_DIR)
        Path(base_dir).mkdir(parents=True, exist_ok=True)

        client_impl = str(cfg.get("client_impl") or "api").strip()
        if client_impl not in ("api", "html"):
            client_impl = "api"
        normalize_zh = str(cfg.get("normalize_zh") or "").strip() or None
        suffix = str(cfg.get("image_suffix") or "").strip() or None
        proxy = str(cfg.get("proxy") or "").strip()
        domain = str(cfg.get("domain") or "").strip()

        def _to_int(value: Any, default: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        option_dict: Dict[str, Any] = {
            "log": bool(cfg.get("jm_log", False)),
            "dir_rule": {
                "rule": "Bd_Aid_Atitle",
                "base_dir": base_dir,
                "normalize_zh": normalize_zh,
            },
            "download": {
                "cache": True,
                "image": {"decode": True, "suffix": suffix},
                "threading": {
                    "image": _to_int(cfg.get("thread_image"), 20),
                    "photo": _to_int(cfg.get("thread_photo"), 3),
                },
            },
            "client": {
                "cache": None,
                "domain": [],
                "impl": client_impl,
                "async_impl": "async_api",
                "retry_times": _to_int(cfg.get("retry_times"), 5),
                "postman": {
                    "type": "curl_cffi",
                    "meta_data": {
                        "impersonate": "chrome",
                        "headers": None,
                        "proxies": None,
                    },
                },
            },
            "plugins": {"dependencies_strategy": "failed-fast", "valid": "log"},
        }
        send_mode = str(cfg.get("send_mode") or "zip").strip()
        if send_mode not in ("zip", "pdf", "images", "none"):
            send_mode = "zip"
        keep_files = bool(cfg.get("keep_files", True))
        if send_mode == "zip":
            option_dict["plugins"]["after_album"] = [{
                "plugin": "zip",
                "kwargs": {
                    "suffix": "zip",
                    "zip_dir": base_dir,
                    "filename_rule": "Atitle",
                    "level": "album",
                    "delete_original_file": not keep_files,
                },
            }]
        elif send_mode == "pdf":
            option_dict["plugins"]["after_album"] = [{
                "plugin": "img2pdf",
                "kwargs": {
                    "pdf_dir": base_dir,
                    "filename_rule": "Atitle",
                    "delete_original_file": not keep_files,
                },
            }]
        if send_mode in ("zip", "pdf"):
            self._ensure_file_server(base_dir)
        if domain:
            option_dict["client"]["domain"] = [
                d.strip() for d in re.split(r"[,，\n]+", domain) if d.strip()
            ]
        if proxy:
            option_dict["client"]["postman"]["meta_data"]["proxies"] = {
                "http": proxy,
                "https": proxy,
            }

        text = yaml.safe_dump(option_dict, allow_unicode=True, sort_keys=False)
        return jmcomic.create_option_by_str(text)

    async def _get_album_detail(self, album_id: str):
        """用异步客户端查询本子详情。"""
        option = self._build_option()
        client = option.new_jm_async_client()
        try:
            return await client.get_album_detail(album_id)
        finally:
            try:
                await client.close()
            except Exception:
                pass

    # ---------- 内置文件服务（zip/pdf 发送模式） ----------

    def _ensure_file_server(self, base_dir: str) -> None:
        """
        在 astrbot 容器内启动 python http.server 托管下载目录，
        供 NapCat 通过 http://<file_server_host>:<port>/... 拉取文件后发送到 QQ。
        """
        try:
            port = int(self.config.get("file_server_port") or 18766)
        except (TypeError, ValueError):
            port = 18766
        if port < 1 or port > 65535:
            port = 18766
        if (self._file_server_proc is not None
                and self._file_server_dir == base_dir
                and self._file_server_port == port):
            if self._file_server_proc.poll() is None:
                return
        self._stop_file_server()
        self._file_server_dir = base_dir
        self._file_server_port = port
        try:
            self._file_server_proc = subprocess.Popen(
                [sys.executable, "-m", "http.server", str(port),
                 "--bind", "0.0.0.0", "--directory", str(base_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"[jmcomic] 文件服务已启动 http://0.0.0.0:{port} → {base_dir}")
        except Exception as e:
            logger.error(f"[jmcomic] 文件服务启动失败: {e}")
            self._file_server_proc = None

    def _stop_file_server(self) -> None:
        if self._file_server_proc is not None:
            try:
                self._file_server_proc.terminate()
            except Exception:
                pass
            try:
                self._file_server_proc.wait(timeout=3)
            except Exception:
                try:
                    self._file_server_proc.kill()
                except Exception:
                    pass
            self._file_server_proc = None
        self._file_server_dir = None

    async def _send_file(self, umo: str, file_path: str) -> bool:
        """通过内置文件服务把本地文件以 URL 形式交给 NapCat 发送。"""
        try:
            base_dir = self._file_server_dir
            if not base_dir:
                return False
            rel = os.path.relpath(file_path, base_dir)
            url_path = quote(rel.replace(os.sep, "/"))
            host = str(self.config.get("file_server_host") or "astrbot").strip() or "astrbot"
            port = self._file_server_port
            url = f"http://{host}:{port}/{url_path}"
            mc = MessageChain(chain=[File(name=os.path.basename(file_path), url=url)])
            await self.context.send_message(umo, mc)
            return True
        except Exception as e:
            logger.error(f"[jmcomic] 发送文件失败: {e}")
            return False

    async def _send_images(self, umo: str, image_files: List[str]) -> None:
        """逐张发送本地图片（Image 组件会转 base64，跨容器可用）。"""
        files = image_files[:MAX_IMAGES_SEND]
        for path in files:
            try:
                await self.context.send_message(
                    umo, MessageChain(chain=[Image.fromFileSystem(path)])
                )
            except Exception as e:
                logger.error(f"[jmcomic] 发送图片失败: {e}")
            await asyncio.sleep(0.4)
        extra = len(image_files) - len(files)
        if extra > 0:
            try:
                await self.context.send_message(
                    umo, MessageChain().message(
                        f"图片较多，已发送前 {len(files)} 张，其余 {extra} 张保存在服务器。"
                    )
                )
            except Exception as e:
                logger.error(f"[jmcomic] 发送提示失败: {e}")

    # ---------- 指令组 ----------

    @filter.command_group("jm", alias={"禁漫"})
    def jm(self):
        """禁漫下载助手指令组"""
        pass

    @jm.command("download", alias={"dl", "下载"})
    async def download(self, event: AstrMessageEvent, content: str = ""):
        """下载本子/章节（后台执行，完成后通知）。"""
        album_ids, photo_ids = _parse_ids(content)
        if not album_ids and not photo_ids:
            yield event.plain_result(
                "未识别到车号。用法：jm download 车号 [p章节] [车号...]，",
                "例如：jm download 123 或 jm download 123 p456"
            )
            return

        all_ids = album_ids + photo_ids
        for info in self._tasks.values():
            dup = [aid for aid in all_ids if aid in info["ids"]]
            if dup:
                yield event.plain_result(
                    f"车号 {', '.join(dup)} 已有任务（{info['desc']}）在下载中，请稍候或先取消。"
                )
                return

        try:
            option = await asyncio.to_thread(self._build_option)
        except Exception as e:
            yield event.plain_result(f"配置初始化失败：{_truncate(e, 200)}")
            return

        desc_parts = [f"album:{a}" for a in album_ids] + [f"photo:p{p}" for p in photo_ids]
        desc = ", ".join(desc_parts)
        key = f"{int(time.time() * 1000)}:{desc}"
        control = jmcomic.DownloadControl()
        task = asyncio.create_task(
            self._download_task(event.unified_msg_origin, key, album_ids, photo_ids, option, control)
        )
        self._tasks[key] = {
            "task": task,
            "control": control,
            "started": time.time(),
            "desc": desc,
            "ids": all_ids,
        }
        yield event.plain_result(f"⏬ 已开始下载 {desc}，完成后会通知你。")

    @jm.command("info", alias={"信息", "详情", "mv"})
    async def info(self, event: AstrMessageEvent, jmid: str):
        """查看本子详情（不下载）。"""
        album_id = _first_id(jmid)
        if not album_id:
            yield event.plain_result("未识别到车号。用法：jm info 车号，例如：jm info 123")
            return
        try:
            album = await self._get_album_detail(album_id)
        except Exception as e:
            logger.error(f"[jmcomic] 查询详情失败: {e}")
            yield event.plain_result(f"查询失败：{_truncate(e, 200)}")
            return
        yield event.plain_result(self._format_album(album))

    @jm.command("search", alias={"搜索"})
    async def search(self, event: AstrMessageEvent, content: str = ""):
        """站内搜索本子。"""
        content = (content or "").strip()
        if not content:
            yield event.plain_result("用法：jm search 关键词 [页码]，例如：jm search 无修正")
            return
        parts = content.split()
        if len(parts) >= 2 and parts[-1].isdigit():
            query, page = " ".join(parts[:-1]), int(parts[-1])
        else:
            query, page = content, 1
        query = _truncate(query, 60)
        try:
            option = await asyncio.to_thread(self._build_option)
            client = option.new_jm_async_client()
            try:
                result = await client.search_site(query, page=page)
            finally:
                try:
                    await client.close()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[jmcomic] 搜索失败: {e}")
            yield event.plain_result(f"搜索失败：{_truncate(e, 200)}")
            return

        items = []
        for aid, title, tags in result.iter_id_title_tag():
            items.append((aid, title, tags))
            if len(items) >= 10:
                break
        if not items:
            yield event.plain_result(f"没有搜到「{query}」相关结果。")
            return

        lines = [f"🔍 「{query}」搜索结果（第 {page} 页，共约 {result.total} 条）："]
        for aid, title, tags in items:
            tag_str = ", ".join(tags[:4]) if tags else ""
            line = f"[{aid}] {title}"
            if tag_str:
                line += f"  |  {tag_str}"
            lines.append(line)
        lines.append("下载：jm download 车号")
        yield event.plain_result("\n".join(lines))

    @jm.command("rank", alias={"排行"})
    async def rank(self, event: AstrMessageEvent, period: str = "月"):
        """查看排行榜（默认本月）。"""
        period = (period or "月").strip()
        try:
            option = await asyncio.to_thread(self._build_option)
            client = option.new_jm_async_client()
            try:
                if period in ("日", "t", "day"):
                    result, label = await client.day_ranking(page=1), "今日"
                elif period in ("周", "w", "week"):
                    result, label = await client.week_ranking(page=1), "本周"
                else:
                    result, label = await client.month_ranking(page=1), "本月"
            finally:
                try:
                    await client.close()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[jmcomic] 排行查询失败: {e}")
            yield event.plain_result(f"排行查询失败：{_truncate(e, 200)}")
            return

        items = [(aid, title) for aid, title in result.iter_id_title()][:10]
        if not items:
            yield event.plain_result(f"{label}排行暂无数据。")
            return
        lines = [f"🏆 {label}排行榜 TOP{len(items)}："]
        for index, (aid, title) in enumerate(items, 1):
            lines.append(f"{index}. [{aid}] {_truncate(title, 40)}")
        yield event.plain_result("\n".join(lines))

    @jm.command("list", alias={"列表", "状态"})
    async def jm_list(self, event: AstrMessageEvent):
        """查看进行中的下载任务。"""
        if not self._tasks:
            yield event.plain_result("当前没有进行中的下载任务。")
            return
        now = time.time()
        lines = ["⏳ 进行中的下载任务："]
        for info in self._tasks.values():
            elapsed = int(now - info["started"])
            lines.append(f"· {info['desc']}（已进行 {elapsed}s）")
        yield event.plain_result("\n".join(lines))

    @jm.command("cancel", alias={"取消"})
    async def cancel(self, event: AstrMessageEvent, jmid: str):
        """取消包含指定车号的下载任务。"""
        target = _first_id(jmid)
        if not target:
            yield event.plain_result("请提供要取消的车号。用法：jm cancel 车号，例如：jm cancel 123")
            return
        for key, info in list(self._tasks.items()):
            if target in info["ids"]:
                info["control"].cancel("用户主动取消")
                yield event.plain_result(f"已请求取消任务 {info['desc']}，正在停止…")
                return
        yield event.plain_result(f"没有找到包含车号 {target} 的进行中任务。")

    @jm.command("help", alias={"帮助", "?"})
    async def help_cmd(self, event: AstrMessageEvent):
        """显示帮助。"""
        yield event.plain_result(HELP_TEXT)

    # ---------- 内部：后台下载 ----------

    async def _download_task(
        self,
        umo: str,
        key: str,
        album_ids: List[str],
        photo_ids: List[str],
        option,
        control,
    ):
        """后台执行下载，完成后按发送模式把内容发回原会话。"""
        export_files: List[str] = []
        image_files: List[str] = []
        try:
            lines: List[str] = []
            if album_ids:
                res = await jmcomic.download_album_async(
                    album_ids, option=option, control=control, check_exception=False
                )
                self._collect_result(lines, res, export_files, image_files)
            if photo_ids:
                res = await jmcomic.download_photo_async(
                    photo_ids, option=option, control=control, check_exception=False
                )
                self._collect_result(lines, res, export_files, image_files)
            msg = "\n".join(lines) if lines else "下载任务已完成，但没有返回结果。"
        except asyncio.CancelledError:
            msg = "下载任务已被取消。"
        except Exception as e:
            logger.error(f"[jmcomic] 下载任务异常: {e}")
            msg = f"下载失败：{_truncate(e, 200)}"
        finally:
            self._tasks.pop(key, None)

        send_mode = str(self.config.get("send_mode") or "zip").strip()
        if send_mode not in ("zip", "pdf", "images", "none"):
            send_mode = "zip"
        try:
            if send_mode == "images" and image_files:
                await self._send_images(umo, image_files)
            elif send_mode in ("zip", "pdf") and export_files:
                sent = 0
                for path in export_files:
                    if await self._send_file(umo, path):
                        sent += 1
                    else:
                        msg += f"\n⚠️ 文件 {os.path.basename(path)} 发送失败，已保存在服务器。"
                if sent:
                    msg += f"\n已发送 {sent} 个文件到当前会话。"
            await self.context.send_message(umo, MessageChain().message(msg))
        except Exception as e:
            logger.error(f"[jmcomic] 发送下载结果失败: {e}")

    def _collect_result(self, lines: List[str], res: Any,
                        export_files: List[str], image_files: List[str]) -> None:
        """把下载结果（单个或批量）整理成文本行，并收集可发送的文件。"""

        def _process(item: Any) -> None:
            detail = getattr(item, "detail", item)
            lines.append(self._format_downloaded(detail))
            manifest = getattr(item, "manifest", None)
            if manifest is not None:
                export_files.extend(manifest.get_export_filepath_list("zip"))
                export_files.extend(manifest.get_export_filepath_list("pdf"))
                image_files.extend(manifest.image_filepath_list)

        if isinstance(res, jmcomic.BatchResult):
            for item in res.succeeded:
                _process(item)
            for aid, err in res.failed.items():
                lines.append(f"❌ [{aid}] {_truncate(err, 150)}")
            return
        _process(res)

    @staticmethod
    def _format_downloaded(detail) -> str:
        """格式化单个下载结果（本子或章节）。"""
        title = getattr(detail, "title", "") or getattr(detail, "name", "")
        aid = getattr(detail, "id", "?")
        save_path = getattr(detail, "save_path", "") or ""
        try:
            is_photo = detail.is_photo()
        except Exception:
            is_photo = False
        head = "✅ 章节下载完成" if is_photo else "✅ 本子下载完成"
        return f"{head} [{aid}] {title}\n   保存至：{save_path}"

    @staticmethod
    def _format_album(album) -> str:
        """格式化本子详情文本。"""
        authors = ", ".join(album.authors) if getattr(album, "authors", None) else "-"
        lines = [
            f"📖 {album.title}",
            f"🆔 JM{album.id}",
            f"✍️ 作者：{authors}",
            f"📅 发布：{album.pub_date}    更新：{album.update_date}",
            f"📄 总页数：{album.page_count}    👀 {album.views}   ❤️ {album.likes}   💬 {album.comment_count}",
        ]
        if getattr(album, "tags", None):
            tags = ", ".join(album.tags[:10])
            lines.append(f"🏷️ {tags}" + (" …" if len(album.tags) > 10 else ""))
        if getattr(album, "actors", None):
            actors = ", ".join(album.actors[:8])
            lines.append(f"🎭 {actors}" + (" …" if len(album.actors) > 8 else ""))
        if getattr(album, "works", None):
            lines.append(f"📚 {', '.join(album.works[:5])}")
        episodes = list(getattr(album, "episode_list", None) or [])
        lines.append(f"📑 章节（{len(episodes)}）：")
        for pid, pindex, ptitle in episodes[:12]:
            lines.append(f"    第{pindex}話 {_truncate(ptitle, 30)} (id: {pid})")
        if len(episodes) > 12:
            lines.append(f"    … 共 {len(episodes)} 话")
        return "\n".join(lines)

    async def terminate(self):
        """插件卸载/停用时取消所有进行中的下载任务并停止文件服务。"""
        for info in self._tasks.values():
            try:
                info["control"].cancel("插件卸载")
                info["task"].cancel()
            except Exception:
                pass
        self._stop_file_server()
