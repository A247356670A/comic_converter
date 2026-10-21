import os
import re

def rename_folders(base_folder, insert_folder, new_name):
    # 筛选符合“第XX话”格式的文件夹
    folders = sorted(
        [f for f in os.listdir(base_folder) if os.path.isdir(os.path.join(base_folder, f)) and re.match(r'^第\d+话$', f)],
        key=lambda name: int(re.search(r'\d+', name).group())
    )

    if insert_folder not in folders:
        print(f"未找到文件夹: {insert_folder}")
        return

    insert_index = folders.index(insert_folder)
    insert_number = int(re.search(r'\d+', new_name).group())  # 新文件夹编号

    # 重命名插入文件夹
    old_path = os.path.join(base_folder, insert_folder)
    new_path = os.path.join(base_folder, new_name)
    os.rename(old_path, new_path)
    print(f"重命名 {old_path} -> {new_path}")

    # 更新文件夹列表并处理剩余文件夹
    current_number = insert_number  # 当前编号起始点
    for i in range(insert_index + 1, len(folders)):
        old_name = folders[i]
        current_number += 1  # 递增新编号
        new_folder_name = f"第{current_number:02d}话"

        old_path = os.path.join(base_folder, old_name)
        new_path = os.path.join(base_folder, new_folder_name)

        os.rename(old_path, new_path)
        print(f"重命名 {old_path} -> {new_path}")

if __name__ == "__main__":
    base_folder = r"Z:\Manga\葬送的芙莉莲_山田钟人_アベツカサ"  # 当前文件夹
    insert_folder = "第113话"  # 要重命名的文件夹
    new_name = "第111话"  # 新文件夹名
    rename_folders(base_folder, insert_folder, new_name)
