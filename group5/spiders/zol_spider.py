import requests
import sqlite3
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
}

MAX_WORKERS = 4
DB_FILE = "zol_goods.db"

url_list = [
    {"category": "手机排行榜", "url": "https://top.zol.com.cn/compositor/57/cell_phone.html"},
    {"category": "笔记本电脑排行榜", "url": "https://top.zol.com.cn/compositor/16/notebook.html"},
    {"category": "台式电脑排行榜", "url": "https://top.zol.com.cn/compositor/27/desktop_pc.html"},
    {"category": "平板电脑排行榜", "url": "https://top.zol.com.cn/compositor/702/tablepc.html"},
    {"category": "液晶显示器排行榜", "url": "https://top.zol.com.cn/compositor/84/lcd.html"},
    {"category": "主板排行榜", "url": "https://top.zol.com.cn/compositor/5/motherboard.html"},
    {"category": "CPU排行榜", "url": "https://top.zol.com.cn/compositor/28/cpu.html"}
]

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    create_sql = '''
    CREATE TABLE IF NOT EXISTS zol_goods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        goods_name TEXT NOT NULL,
        UNIQUE(category, goods_name)
    )
    '''
    cur.execute(create_sql)
    conn.commit()
    cur.close()
    conn.close()

def batch_insert(category, goods_list):
    if not goods_list:
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    data = [(category, name) for name in goods_list]
    insert_sql = "INSERT OR IGNORE INTO zol_goods (category, goods_name) VALUES (?, ?)"
    cur.executemany(insert_sql, data)
    conn.commit()
    cur.close()
    conn.close()

def crawl(category, url):
    goods_list = []
    try:
        res = requests.get(url, headers=headers, timeout=15)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")

        goods_items = soup.select("div.rank-list__item div.rank-list__cell.cell-3 div.rank__name a")
        if not goods_items:
            print(f"{category} 无数据")
            return

        for a_tag in goods_items:
            name = a_tag.get_text(strip=True)
            goods_list.append(name)
        
        batch_insert(category, goods_list)
    except Exception as e:
        print(f"{category} 爬取失败：{str(e)}")

if __name__ == "__main__":
    init_db()
    print("开始爬取并入库...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for item in url_list:
            executor.submit(crawl, item["category"], item["url"])
    print("爬取完成，数据已全部存入")
