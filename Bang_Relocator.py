import os
import shutil


def move_files_from_subfolder(base_folder, target_folder_name="全一話"):
    for root, dirs, _ in os.walk(base_folder):
        for dir_name in dirs:
            if dir_name == target_folder_name:
                target_folder_path = os.path.join(root, dir_name)
                parent_folder_path = os.path.dirname(target_folder_path)

                # 移动文件
                for file_name in os.listdir(target_folder_path):
                    source_file = os.path.join(target_folder_path, file_name)
                    destination_file = os.path.join(parent_folder_path, file_name)

                    if os.path.isfile(source_file):
                        print(f"Moving: {source_file} -> {destination_file}")
                        shutil.move(source_file, destination_file)

                # 删除空文件夹
                print(f"Removing empty folder: {target_folder_path}")
                os.rmdir(target_folder_path)


if __name__ == "__main__":
    # base_folder = os.getcwd()  # 当前文件夹
    move_files_from_subfolder(r'Z:\Manga\Downloads\BanG Dream!')
