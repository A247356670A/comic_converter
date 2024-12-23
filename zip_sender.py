import os
import shutil


def move_zip_files(base_path):
    """
    遍历当前文件夹，将同名ZIP文件剪切到同名文件夹内，覆盖已存在的文件。
    """
    files = os.listdir(base_path)
    zip_files = {f[:-4]: f for f in files if f.lower().endswith('.zip')}
    folders = {f: os.path.join(base_path, f) for f in files if os.path.isdir(os.path.join(base_path, f))}

    for folder_name, folder_path in folders.items():
        if folder_name in zip_files:
            zip_file = zip_files[folder_name]
            src_path = os.path.join(base_path, zip_file)
            dest_path = os.path.join(folder_path, zip_file)

            try:
                # 移动ZIP文件，覆盖已有文件
                shutil.move(src_path, dest_path)
                print(f"移动: {src_path} -> {dest_path} (覆盖已存在的文件)")
            except Exception as e:
                print(f"移动 {src_path} 到 {dest_path} 失败: {e}")


def main():
    current_folder = r"Z:\Manga"  # 获取当前文件夹路径
    print(f"处理文件夹: {current_folder}")
    move_zip_files(current_folder)


if __name__ == "__main__":
    main()
