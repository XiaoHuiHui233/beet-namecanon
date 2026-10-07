# beet-namecanon

beets 插件：把被标成**本地译名**的艺人名规范化回 MusicBrainz 实体的**拉丁规范名**。

```
女神卡卡             → Lady Gaga
比莉艾莉什           → Billie Eilish
查莉 XCX             → Charli xcx
彼得·伊里奇·柴可夫斯基 → Pyotr Ilyich Tchaikovsky     （MB 规范名是西里尔文 → 取 primary en 别名）
犹太祭司             → Judas Priest
Сергей Васильевич Рахманинов → Sergei Rachmaninoff
```

中/港/台/日/韩/澳艺人**一律不动**：`王菲` 不会变成 Faye Wong，`放課後ティータイム` 不会变成
Sakurakou K-ON Bu —— 那些是艺人本国文字，不是译名。

## 为什么不能只看 credit

本地 `albumartists_credit` 是拉丁名**不代表**本地名是译名。实测某音乐库 19 个「中文名 + 拉丁
credit」的艺人里，16 个是中/日艺人用罗马字署名（王菲/Faye Wong、张学友/Jacky Cheung、
张艺兴/LAY、蔡依林/Jolin Tsai、放課後ティータイム/Sakurakou K-ON Bu）。
**只有查 MB 实体的国籍才能区分**，所以本插件一律查实体。

## 判定规则（全部满足才改）

1. MB 实体规范名不含 CJK（含中文/日文的实体直接跳过）；
2. 实体国籍/地区不在 `skip_countries`（默认 CN TW HK JP KR MO）；
3. 本地名**整体**等于实体规范名或它的某个别名（比较前做繁→简 + 去空白 + 小写归一）；
4. 实体不是 MB 的占位艺人（`[unknown]`、`[anonymous]`、`[dialogue]` …）。

目标名：规范名是拉丁字母就用它；不是（西里尔/希腊/阿拉伯……）则取 **locale 为 en 的
primary 别名**，没有拉丁别名时保持原文字（MB 里 `Игорь Скляр` 就没有拉丁别名）。

## 安全闸门（都是实际踩过的坑）

- **混合署名不整串替换**：`Luis Fonsi feat. 林俊杰`、`Hardwell feat. 蔡依林`、`K/DA with …刘柏辛Lexie…`
  这类合作署名，本地名不是任何别名 → 一律不动。整串替换会丢掉中文合作者。
- **多艺人曲目按 `mb_artistids` 逐段处理**：`Alla Pugacheva & Ритм` 两个艺人的 MBID 都存在时，
  两段各自查实体、各自转换，不会只转第一段。
- **段内只替换"非拉丁"别名**：`女神卡卡 feat. R. Kelly` → `Lady Gaga feat. R. Kelly`，
  而拉丁别名不会被拿去替换拉丁串，避免误伤。
- **别名匹配做繁简归一**：MB 别名常是繁体（`猶太祭司`），本地是简体（`犹太祭司`），
  不归一会出现「只改了 artists/sort、artist 没跟上」的半吊子状态。
- **占位艺人不碰**：`［未知］` 比 `[unknown]` 好读，且占位符不是艺人名。

## 安装

```bash
pip install git+https://github.com/XiaoHuiHui233/beet-namecanon.git
# 或本地开发
pip install -e .
```

也可以不做 pip 安装，直接把 `namecanon/` 放进 beets 的插件目录：

```yaml
pluginpath:
    - /path/to/plugins          # 该目录下需有 namecanon/__init__.py
plugins: ... namecanon
```

## 配置

```yaml
namecanon:
    cache_file: /data/beets/no_mbid_analysis/artist_canon.json   # MB 查询缓存
    skip_countries: CN TW HK JP KR MO                            # 这些国籍的艺人不动
    move: yes                                                    # 改名后同步移动文件/目录
    request_delay: 1.1                                           # MB API 间隔(秒)，别调太小
    search_fallback: yes                                         # 无 MBID 时用别名反查实体
```

## 用法

```bash
beet namecanon              # 全库
beet namecanon <query>      # 只处理匹配的专辑，如 beet namecanon 女神卡卡 / id:4605
```

输出（每周任务的摘要行就抓这一行）：

```
namecanon: 规范化 12 处 / 6 张专辑 (未判定 0 张)
   [4605] 女神卡卡 → Lady Gaga  (ARTPOP)
```

触发方式：

- `import_task` 钩子：新导入的专辑自动处理（写了 `mb_albumartistid` 时）；
- 手工/定时跑 `beet namecanon`。建议排在 mbsync 之后（`mbsync → simptrans → namecanon`），
  否则每周从 MB 拉回的元数据可能覆盖掉前面的结果。

## 缓存

按 MBID 缓存实体的规范名/排序名/国籍/地区/别名（JSON），缓存命中不发网络请求。
首次全库约 1300 次查询 ≈ 40 分钟（每 50 次落一次盘）；之后每周离线跑完约 **8 秒**。
只有出现**新艺人**时才会联网补查。

## 已知限制

- MB 没有拉丁别名的艺人无法拉丁化（例：`Игорь Скляр`、`Ольга Восконьян` 保持西里尔文）。
  需要时可加 `namecanon.manual_map`（TODO）或直接在 MB 上补别名。
- 只处理艺人名字段（`albumartist`/`albumartists`/`artist`/`artists` 及对应 sort），
  不动专辑名与曲目名（那属于 simptrans 的繁简规则）。
- 依赖 `zhconv` 做繁简归一（simptrans 同款依赖）。
