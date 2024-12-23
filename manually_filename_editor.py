import os

# 指定漫画文件夹路径
base = r"Z:\Manga\Downloads\zh.dmzj\54892"


def get_sorted_chapters(folder_path):
    """
    获取按创建时间倒序排列的章节文件夹列表。
    """
    chapters = [
        (chapter, os.path.getmtime(os.path.join(folder_path, chapter)))
        # (chapter, os.path.getctime(os.path.join(folder_path, chapter)))

        for chapter in os.listdir(folder_path)
        if os.path.isdir(os.path.join(folder_path, chapter))
    ]
    # 按时间倒序排列
    return sorted(chapters, key=lambda x: x[1], reverse=False)


def rename_chapters(folder_path):
    sorted_chapters = get_sorted_chapters(folder_path)
    print("找到的章节文件夹：")
    for chapter, _ in sorted_chapters:
        print(f"- {chapter}")

    start_index = -1

    # 找到“第01话”的索引
    for idx, (chapter, _) in enumerate(sorted_chapters):
        if chapter.startswith("第01话"):
            start_index = idx
            break

    if start_index == -1:
        print(f"未找到“第01话”文件夹，跳过文件夹 {folder_path}")
        return

    # 按顺序重命名后续文件夹
    counter = 2
    for chapter, _ in sorted_chapters[start_index + 1:]:
        if chapter.startswith("第") and chapter.endswith("话"):
            print(f"文件夹 {chapter} 已被重命名，跳过。")
            continue

        new_name = f"第{counter:02d}话"
        old_path = os.path.join(folder_path, chapter)
        new_path = os.path.join(folder_path, new_name)

        # 检查命名冲突，避免中断
        while os.path.exists(new_path):
            counter += 1
            new_name = f"第{counter:02d}话"
            new_path = os.path.join(folder_path, new_name)

        print(f"重命名文件夹: {old_path} -> {new_path}")
        os.rename(old_path, new_path)
        counter += 1
    counter = 0


def main():
    if os.path.isdir(base):
        print(f"处理漫画文件夹: {base}")
        rename_chapters(base)
    else:
        print(f"文件夹 {base} 不存在，请检查路径。")


if __name__ == "__main__":
    main()
