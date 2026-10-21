# Aidoku → Komga 自动转换器

双击 `Start_Komga_Converter.bat`，选择输入目录和输出目录，点击“扫描预览”，检查中文漫画名和章节名后点击“开始自动转换”。默认输出到 `Z:\Manga`。

当前下载目录实际是 `Z:\未命名文件夹\Downloads\zh.copymanga`。可以选择 Downloads、zh.copymanga 或单部漫画的目录；会递归查找 CBZ/ZIP。

生成结构：`漫画名\第01话\001.png`。图片会按原文件名中的数字排序，使用 Pillow 解码后写为真正的 PNG（不是改扩展名）。PNG 通常比 WebP 大，请留出空间。ComicInfo.xml 的 Series 和 Title 优先；缺失时访问配置网站的章节页，读取 h4.header。网络超时/验证页/缺失标题会记录失败并继续其他章节。保留“试看”、番外等标记。

源文件不会移动或删除。每章在临时目录完成后才提交到目标目录。重复导入通过源文件和所有输出 PNG 的 SHA-256 校验跳过。已有章节但无法确认一致时报告冲突，绝不覆盖。失败/中止章节清理自己的临时目录；突然断电可能留下 `.converting-*` 临时目录，勿当作完成章节。停止后已完成章节保留，下次可继续。

转换结束在输出根目录生成 `conversion-report-*.json`。本工具只生成图片目录，不会修改 Komga 账户、数据库或触发扫描；按现有 Komga 书库配置扫描即可。新漫画是否位于现有书库扫描范围内取决于你的书库根目录设置。

如现有漫画目录名称与元数据不同，可建立 JSON 名称映射，在界面中选择，例如：

```json
{
  "xianggelilafulongtiyashizuolierenxiangshenzuofaqit": "你现有的漫画目录名"
}
```

键支持漫画 slug 或原始中文漫画名；值为输出目录名。不会模糊匹配或自动改动已有目录。

依赖：Python 3.11+（含 Tkinter）、Pillow。安装：`python -m pip install -r requirements-komga.txt`。

命令行仅预览：
```powershell
python aidoku_to_komga.py --input 'Z:\未命名文件夹\Downloads\zh.copymanga' --output 'Z:\Manga' --offline
```
增加 `--convert` 执行。默认联网仅用于缺失元数据时；`--offline` 完全禁用联网；`--mapping names.json` 指定名称映射；`--base-url https://www.copy20.com` 设置补全网站。

不支持密码压缩包、多帧图片或无编号且顺序不明的图片语义；无数字时按文件名排序，请确认预览和来源。压缩包上限 8 GiB 解压大小、单图 256 MiB、10000 个条目。原有项目脚本保持原样，新工具不调用原项目中的删除或移动脚本。
