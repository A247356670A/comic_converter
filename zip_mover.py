import os
import shutil


def move_zip_files(source_folder, destination_folder):
    # 遍历源文件夹内的所有文件和子文件夹
    for root, dirs, files in os.walk(source_folder):
        for file in files:
            # 检查文件扩展名是否为.zip
            if file.lower().endswith('.zip'):
                source_path = os.path.join(root, file)
                destination_path = os.path.join(destination_folder, file)

                try:
                    # 剪贴文件
                    shutil.move(source_path, destination_path)
                    print(f"已移动: {source_path} -> {destination_path}")
                except Exception as e:
                    print(f"无法移动 {source_path}: {e}")


if __name__ == "__main__":
    source_folder = input("请输入源文件夹路径: ").strip()
    # destination_folder = input("请输入目标文件夹路径: ").strip()

    if os.path.isdir(source_folder):
        move_zip_files(source_folder, source_folder)
    else:
        print("源文件夹路径无效！")