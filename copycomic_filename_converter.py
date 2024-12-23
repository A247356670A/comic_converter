import os
import time
import re
import requests
from bs4 import BeautifulSoup

# 定义主目录
base_folder = r"Z:\Manga\Downloads\zh.copymanga"
base_url = "https://www.copymanga.tv/comic"

# 创建保存结果的文件
output_file = r"Z:\Manga\manga_titles.txt"


def load_existing_titles(output_file):
    """
    读取已存在的manga_titles.txt文件，返回已处理的URL集合。
    """
    processed_urls = set()
    if os.path.exists(output_file):
        with open(output_file, "r", encoding="utf-8") as file:
            lines = file.readlines()
            for i in range(0, len(lines), 3):
                url = lines[i].strip()
                if url:
                    processed_urls.add(url)
    return processed_urls


def get_h4_content(url):
    """
    从指定URL抓取<h4>标签的内容。
    """
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        h4_content = soup.find("h4", class_="header")
        return h4_content.text.strip() if h4_content else None
    except requests.exceptions.HTTPError as e:
        print(f"Error fetching {url}: {e}")
        if e.response.status_code == 404:
            return None  # 返回None以继续跳过逻辑
    except Exception as e:
        print(f"Unexpected error fetching {url}: {e}")
        return None


def replace_subfolders_names(base_folder, manga_id, chapter_id, h4_content):
    """
    使用<h4>内容更新漫画名文件夹和章节名文件夹。
    """
    if not h4_content:
        return

    # 提取漫画名和章节名
    if "/" in h4_content:
        manga_name, chapter_name = h4_content.split("/", 1)
        chapter_name = chapter_name.strip()
    else:
        chapter_name = "Unknown Chapter"

    manga_path = os.path.join(base_folder, manga_id)
    chapter_path = os.path.join(manga_path, chapter_id)

    # 更新章节名文件夹
    new_chapter_path = os.path.join(manga_path, chapter_name)
    if chapter_path != new_chapter_path and not os.path.exists(new_chapter_path):
        print(f"Renaming chapter folder: {chapter_path} -> {new_chapter_path}")
        os.rename(chapter_path, new_chapter_path)


def sanitize_folder_name(name):
    """
    删除Windows文件夹名称中不允许的字符。
    """
    return re.sub(r'[<>:"/\\|?*]', '_', name)

def replace_folder_names(base_folder, manga_id, h4_content):
    """
    使用<h4>内容更新漫画名文件夹和章节名文件夹。
    """
    if not h4_content:
        return

    # 提取漫画名和章节名
    if "/" in h4_content:
        manga_name, _ = h4_content.split("/", 1)
        manga_name = manga_name.strip()
    else:
        manga_name = h4_content.strip()

    manga_name = sanitize_folder_name(manga_name)
    manga_path = os.path.join(base_folder, manga_id)
    new_manga_path = os.path.join(base_folder, manga_name)

    # 更新漫画名文件夹
    if manga_path != new_manga_path and not os.path.exists(new_manga_path):
        print(f"Renaming manga folder: {manga_path} -> {new_manga_path}")
        try:
            os.rename(manga_path, new_manga_path)
        except OSError as e:
            print(f"Error renaming folder: {e}")


def main():
    # 读取已处理的URL集合
    # processed_urls = load_existing_titles(output_file)

    # 打开结果文件
    with open(output_file, "a", encoding="utf-8") as file:
        # 遍历主文件夹下的所有漫画文件夹
        for manga_id in os.listdir(base_folder):
            print("manga_id: ", manga_id)
            manga_path = os.path.join(base_folder, manga_id)
            if not os.path.isdir(manga_path):
                continue
            h4_content = None  # 初始化为 None
            # 跳过无效的系统文件（如 .DS_Store）
            if manga_id.startswith("."):
                continue
            # 遍历漫画文件夹中的章节子文件夹
            for chapter_id in os.listdir(manga_path):
                print("chapter_id: ", chapter_id)
                chapter_path = os.path.join(manga_path, chapter_id)
                if not os.path.isdir(chapter_path):
                    continue
                if chapter_id.startswith("."):
                    continue
                # 构建目标URL
                url = f"{base_url}/{manga_id}/chapter/{chapter_id}"
                # if url in processed_urls:
                #     print(f"Skipping already processed URL: {url}")
                    # continue  # 跳过已处理的URL

                print(f"Fetching data from {url}")

                # 抓取并提取<h4>内容
                h4_content = get_h4_content(url)
                if h4_content:
                    print(f"Extracted: {h4_content}")
                    file.write(f"{url}\n{h4_content}\n\n")
                    replace_subfolders_names(base_folder, manga_id, chapter_id, h4_content)
                    time.sleep(1)
                else:
                    print(f"Failed to extract data from {url}")
            if h4_content:
                replace_folder_names(base_folder, manga_id, h4_content)


if __name__ == "__main__":
    main()
