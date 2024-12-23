import os

def delete_zip_files(base_path):
    """
    递归删除当前文件夹及其子文件夹中的所有ZIP文件。
    """
    for root, _, files in os.walk(base_path):
        for file in files:
            if file.lower().endswith('.zip'):
                zip_file_path = os.path.join(root, file)
                try:
                    os.remove(zip_file_path)
                    print(f"已删除: {zip_file_path}")
                except Exception as e:
                    print(f"删除失败: {zip_file_path} - 错误: {e}")

def main():
    current_folder = r"Z:\Manga"  # 获取当前工作目录
    print(f"开始删除 {current_folder} 及其子文件夹中的ZIP文件...")
    delete_zip_files(current_folder)
    print("删除完成！")

if __name__ == "__main__":
    main()
