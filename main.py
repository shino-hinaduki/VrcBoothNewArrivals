import os
import datetime
import requests
import shutil
import glob
import json
import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi_utils.tasks import repeat_every
from bs4 import BeautifulSoup
from PIL import Image
from discord_webhook import DiscordWebhook, DiscordEmbed


def get_config_or_default(key: str, default_value: object) -> object:
    """環境変数もしくは引数の設定値を取得

    Args:
        key (str): 環境変数名
        default_value (object): 未定義時の値

    Returns:
        object: 環境変数、もしくは設定値
    """
    if key in os.environ:
        return os.getenv(key)
    else:
        return default_value


def get_booth_items_url() -> str:
    """対象URLを取得

    Returns:
        str: Booth商品ページURL
    """
    return get_config_or_default(
        "VBNA_TARGET_URL", "https://booth.pm/ja/items?sort=new&tags%5B%5D=VRChat"
    )


def get_raw_html(url: str) -> str:
    """指定されたURLから生のhtml textを取得

    Args:
        url (str): 対象URL

    Returns:
        str: HTML
    """
    res = requests.get(url)
    if res.status_code != 200:
        raise ConnectionError(res)
    return res.text


def parse_items(text: str) -> list[dict[str, str]]:
    """Booth商品ページのHTMLから商品情報を抽出

    Args:
        text (str): HTMLのテキスト

    Returns:
        list[dict[str, str]]: 商品ごと `{name, url, image_url, price}` が定義された配列
    """
    soup = BeautifulSoup(text, "html.parser")
    items = soup.select(
        "body > div.page-wrap > main > div.container > div.l-row.l-market-grid.u-mt-0.u-ml-0 > div > div.u-mt-400 > ul > li"
    )

    urls = [
        i.select(
            "div.item-card__thumbnail.js-thumbnail > div.item-card__thumbnail-images > a:nth-child(1)"
        )[0].attrs["href"]
        for i in items
    ]
    image_urls = [
        i.select(
            "div.item-card__thumbnail.js-thumbnail > div.item-card__thumbnail-images > a:nth-child(1)"
        )[0].attrs["data-original"]
        for i in items
    ]
    names = [
        i.select("div.item-card__summary > div.item-card__title > a")[0].text
        for i in items
    ]
    prices = [
        i.select(
            "div.item-card__summary > div.u-d-flex.u-align-items-center.u-justify-content-between > div.price.u-text-primary.u-text-left.u-tpg-caption2"
        )[0].text
        for i in items
    ]

    return [
        {"name": name, "url": url, "image_url": image_url, "price": price}
        for (name, url, image_url, price) in zip(names, urls, image_urls, prices)
    ]


def get_work_dir(category_name: str, need_recreate: bool) -> str:
    """生成物置き場のディレクトリを取得。なければ新規作成

    Args:
        category_name (str): サブディレクトリ名
        need_recreate (bool): ディレクトリを一旦削除する場合はtrue

    Returns:
        str: ディレクトリの絶対パス
    """
    base_dir = get_config_or_default(
        "VBNA_WORK_DIR", os.path.join(os.path.curdir, "tmp")
    )
    if not (os.path.exists(base_dir)):
        os.mkdir(base_dir)

    dst_dir = os.path.join(base_dir, category_name)
    if not (os.path.exists(dst_dir)):
        # create new
        os.mkdir(dst_dir)
    elif need_recreate:
        # remove and recreate
        shutil.rmtree(dst_dir)
        os.mkdir(dst_dir)
    return dst_dir


def get_filename_from_url(url: str) -> str:
    """URLからファイル名を取得

    Args:
        url (str): 対象のURL

    Raises:
        NameError: ファイル名がなかった場合

    Returns:
        str: ファイル名、拡張子付き
    """
    filename = url[url.rfind("/") + 1 :]
    if not (filename):
        raise NameError(f"get_filename_from_url(url={url}) -> {filename}")
    return filename


def download_images(
    category: str, target_urls: list[str], clear_cache: bool = False
) -> list[str]:
    """指定された画像をまとめてDLする

    Args:
        category (str): DL先
        target_urls (list[str]): DL対象
        clear_cache (bool, optional): DL先ディレクトリを事前クリアするか. Defaults to False.

    Raises:
        ConnectionError: DLできなかった場合

    Returns:
        list[str]: DL先のリスト

    Yields:
        Iterator[list[str]]: DL先のリスト
    """
    base_dir = get_work_dir(f"download_cache_{category}", need_recreate=clear_cache)
    for target_url in target_urls:
        dst_name = get_filename_from_url(target_url)
        dst_path = os.path.join(base_dir, dst_name)

        if os.path.exists(dst_path):
            # use from cache
            pass
        else:
            # download
            res = requests.get(target_url)
            if res.status_code != 200:
                raise ConnectionError(res)
            # write to file
            with open(dst_path, mode="wb") as f:
                f.write(res.content)
        yield dst_path


def get_dst_dir(clear_cache: bool = False) -> str:
    """生成物保存先を取得

    Args:
        clear_cache (bool, optional): 事前削除するか. Defaults to False.

    Returns:
        str: 生成物保存先の絶対パス
    """
    dst_dirname = get_config_or_default("VBNA_DST_DIR", "dist")
    dst_dir = get_work_dir(dst_dirname, need_recreate=clear_cache)
    return dst_dir


def create_tile_image(dst_dir: str, local_images: list[str]) -> (str, dict[str, str]):
    """指定された画像をタイル状に並べた画像を生成

    Args:
        dst_dir (_type_): 生成物保存先のディレクトリ
        local_images list[str]: 並べる画像のフルパス

    Returns:
        (str, dict[str, str]): (タイル画像, 画像の付帯情報のdict)
    """
    # dst path
    dst_filename = get_config_or_default("VBNA_DST_IMAGE_NAME", "index.jpg")
    dst_path = os.path.join(dst_dir, dst_filename)
    # image info
    src_width = get_config_or_default("VBNA_SRC_IMAGE_WIDTH", 300)
    src_height = get_config_or_default("VBNA_SRC_IMAGE_HEIGHT", 300)

    dst_width = get_config_or_default("VBNA_DST_IMAGE_WIDTH", 2048)
    dst_height = get_config_or_default("VBNA_DST_IMAGE_HEIGHT", 2048)
    dst_margin = get_config_or_default("VBNA_DST_IMAGE_MARGIN", 0)

    num_columns = dst_width // src_width
    num_rows = dst_height // src_height
    num_items = 0
    # create image
    with Image.new("RGB", (dst_width, dst_height)) as dst_image:
        for i, src_image_path in enumerate(local_images):
            # tile offset
            x = i % num_columns
            y = i // num_columns
            # overrun
            if x >= num_columns or y >= num_rows:
                break
            # paste to tile
            with Image.open(src_image_path) as src_image:
                src_image.resize((src_width, src_height))
                dst_image.paste(
                    src_image,
                    (
                        x * (src_width + dst_margin),
                        dst_height - (y + 1) * (src_height + dst_margin),  # 下から上方向に配置する
                    ),
                )
                num_items += 1
        dst_image.save(dst_path)
    return (
        dst_path,
        {
            "name": dst_filename,
            "src_width": src_width,
            "src_height": src_height,
            "dst_width": dst_width,
            "dst_height": dst_height,
            "dst_margin": dst_margin,
            "num_columns": num_columns,
            "num_rows": num_rows,
        },
    )


def create_info_file(
    dst_dir: str,
    target_url: str,
    items: dict[str, str],
    dst_image_path: str,
    img_info: dict[str, str],
) -> dict[str, str]:
    """付帯情報を生成し、json出力及びWebhook通知する

    Args:
        dst_dir (str): 生成物保存先
        target_url (str): 対象URL
        items (dict[str, str]): 取得したリスト
        dst_image_path (str): 取得した画像リスト
        img_info (dict[str, str]): 画像の付帯情報

    Raises:
        ConnectionError: Webhook通知失敗

    Returns:
        dict[str, str]: 付帯情報
    """
    dst_json_filename = get_config_or_default("VBNA_DST_INFO_NAME", "index.json")
    dst_json_path = os.path.join(dst_dir, dst_json_filename)

    dst = {
        "created_at": str(datetime.datetime.now()),
        "target_url": target_url,
        "items": items,
        "img_info": img_info,
    }
    # save file
    with open(dst_json_path, mode="w", encoding="utf-8") as f:
        json.dump(dst, f, ensure_ascii=False)
    # post webhook
    webhook_url = get_config_or_default("VBNA_WEBHOOK_URL", "")
    if webhook_url:
        webhook = DiscordWebhook(url=webhook_url, content="Data Updated!")
        # image
        with open(dst_image_path, "rb") as f:
            webhook.add_file(file=f.read(), filename=os.path.basename(dst_image_path))
        embed = DiscordEmbed(title="Content Details")
        embed.set_author(name="Booth", url=target_url)
        for i in items:
            embed.add_embed_field(name=i["name"], value=i["url"])
        webhook.add_embed(embed)
        # json
        with open(dst_json_path, "rb") as f:
            webhook.add_file(file=f.read(), filename=os.path.basename(dst_json_path))

        resp = webhook.execute()
        if not (resp.ok):
            raise ConnectionError(resp)
    return dst


def update_data():
    """
    生成データの更新
    """

    # boot message post webhook
    webhook_url = get_config_or_default("VBNA_WEBHOOK_URL", "")
    if webhook_url:
        webhook = DiscordWebhook(url=webhook_url, content=f"Start Update...")
        resp = webhook.execute()
        if not (resp.ok):
            raise ConnectionError(resp)

    logging.info(f"VrcBoothNewArrivals")

    target_url = get_booth_items_url()
    logging.info(f"start download... target_url={target_url}")

    html_txt = get_raw_html(target_url)
    items = parse_items(html_txt)

    local_image_path_arr = list(
        download_images(
            "items", [item["image_url"] for item in items], clear_cache=True
        )
    )

    dst_dir = get_dst_dir()
    logging.info(f"dst_dir={dst_dir}")

    dst_image_path, img_info = create_tile_image(dst_dir, local_image_path_arr)
    info = create_info_file(dst_dir, target_url, items, dst_image_path, img_info)
    logging.info(f"Done.")
    return info


############################################################################################################################
# internal api server setup
logging.basicConfig(level=logging.INFO, format="[VBNA][%(levelname)s]: %(message)s")
# boot message post webhook
webhook_url = get_config_or_default("VBNA_WEBHOOK_URL", "")
if webhook_url:
    webhook = DiscordWebhook(url=webhook_url, content=f"Start Server")
    resp = webhook.execute()
    if not (resp.ok):
        raise ConnectionError(resp)

# start api server
app = FastAPI()
# get_dst_dir内でディレクトリがなければ生成されている
dst_dir = get_dst_dir()
# staticにあるファイルを公開用にコピー
if os.path.exists("static"):
    for src_path in glob.glob("static/**/*", recursive=True):
        shutil.copy(src_path, dst_dir)
# 公開
app.mount("/static", StaticFiles(directory=dst_dir), name="static")


@app.get("/")
async def root():
    """現在のCommit hashなどを返す

    Returns:
        dict[str]: サーバ情報
    """
    return {
        "name": "VrcBoothNewArrivals-internal",
        "json": "/static/index.json",
        "jpg": "/static/index.jpg",
    }


# 定期更新
# Booth serverに迷惑かかると困るので止まる方優先
update_period_seconds = get_config_or_default(
    "VBNA_UPDATE_PERIOD_SECONDS", 12 * 60 * 60
)
# force 1st update
update_on_boot = get_config_or_default("VBNA_UPDATE_ON_BOOT", False)


@app.on_event("startup")
@repeat_every(
    seconds=update_period_seconds,
    logger=logging,
    wait_first=not (update_on_boot),
    raise_exceptions=True,
)
def update():
    """リソースの更新"""
    update_data()
