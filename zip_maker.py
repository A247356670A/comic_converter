import os
import zipfile


def compress_folder(folder_path, output_zip):
    """
    压缩文件夹为ZIP文件，覆盖已存在的ZIP文件。
    """
    try:
        with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(folder_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    relative_path = os.path.relpath(file_path, folder_path)
                    zipf.write(file_path, relative_path)
        print(f"已压缩: {folder_path} -> {output_zip}")
    except Exception as e:
        print(f"压缩失败: {folder_path} - 错误: {e}")


def process_folders(base_path):
    """
    遍历当前文件夹的每个子文件夹的子文件夹，将其压缩为ZIP文件。
    """
    for folder_name in os.listdir(base_path):
        folder_path = os.path.join(base_path, folder_name)
        if os.path.isdir(folder_path):
            print(f"开始压缩 {folder_path} 内的子文件夹...")
            for subfolder_name in os.listdir(folder_path):
                subfolder_path = os.path.join(folder_path, subfolder_name)
                if os.path.isdir(subfolder_path):
                    output_zip = f"{subfolder_path}.zip"

                    # 删除已存在的ZIP文件
                    if os.path.exists(output_zip):
                        print(f"当前话{output_zip}已存在， 替换中...")
                        os.remove(output_zip)

                    # 压缩文件夹
                    compress_folder(subfolder_path, output_zip)


def main():
    current_folder = r"Z:\Manga\Downloads\ToMerge"  # 获取当前文件夹
    process_folders(current_folder)
    print("压缩完成！")


if __name__ == "__main__":
    main()
