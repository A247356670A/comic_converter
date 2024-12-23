import os
import re

def rename_folders(base_folder, insert_folder, new_name):
    folders = sorted(
        [f for f in os.listdir(base_folder) if os.path.isdir(os.path.join(base_folder, f))],
        key=lambda name: int(re.search(r'\d+', name).group())
    )

    if insert_folder not in folders:
        print(f"未找到文件夹: {insert_folder}")
        return

    insert_index = folders.index(insert_folder)

    # 重命名插入文件夹
    old_path = os.path.join(base_folder, insert_folder)
    new_path = os.path.join(base_folder, new_name)
    os.rename(old_path, new_path)
    print(f"重命名 {old_path} -> {new_path}")

    # 更新文件夹列表
    folders = sorted(
        [f for f in os.listdir(base_folder) if os.path.isdir(os.path.join(base_folder, f))],
        key=lambda name: int(re.search(r'\d+', name).group())
    )

    # 重命名之后的文件夹
    for i in range(insert_index + 1, len(folders)):
        old_name = folders[i]
        old_number = int(re.search(r'\d+', old_name).group()) - 1
        new_folder_name = re.sub(r'\d+', f"{old_number:02d}", old_name, count=1)

        old_path = os.path.join(base_folder, old_name)
        new_path = os.path.join(base_folder, new_folder_name)

        os.rename(old_path, new_path)
        print(f"重命名 {old_path} -> {new_path}")

if __name__ == "__main__":
    base_folder = r"Z:\Manga\Downloads\zh.dmzj\54892"  # 当前文件夹
    insert_folder = "第38话"  # 需要插入的文件夹
    new_name = "第37.5话"  # 新文件夹名
    rename_folders(base_folder, insert_folder, new_name)
