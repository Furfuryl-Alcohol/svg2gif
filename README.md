# svg2gif

把动画 SVG 转成 GIF，**可以指定分辨率，也可以直接指定文件大小上限**。

- `svg2gif.py` —— 命令行
- `svg2gif_gui.py` —— 图形界面（tkinter）

支持 **SMIL**（`<animate>` / `<animateTransform>`）和 **CSS 动画**（`@keyframes` +
`animation:`），两种可以同时存在于一个文件里。

## 安装

```bash
pip install -e .          # 或者 pip install playwright pillow
python -m playwright install chromium
```

只需要一次；Chromium 是无头运行的，不会弹窗。

Windows / macOS 的官方 Python 自带 tkinter；Debian/Ubuntu 上跑图形界面需要
`sudo apt install python3-tk`。Linux 容器里 Chromium 常因沙箱起不来，可以：

```bash
SVG2GIF_CHROME_ARGS="--no-sandbox" python svg2gif.py fig.svg fig.gif
```

装成包之后有两个命令：`svg2gif` 和 `svg2gif-gui`。

## 图形界面

```bash
python svg2gif_gui.py              # 空界面
python svg2gif_gui.py fig.svg      # 顺便把文件填进去
```

界面里选源文件 / 输出路径，尺寸三选一（按宽度、按高度、限制文件大小），
再设帧率、色数、循环、背景（透明 / 白色 / 自定义），勾"导出后自检"。
转换在后台线程里跑，日志实时输出，结束后右边显示首帧预览和实际参数。

## 命令行用法

```bash
# 按宽度
python svg2gif.py fig.svg fig.gif --width 1200

# 按高度（宽度按 viewBox 比例自动算）
python svg2gif.py fig.svg fig.gif --height 800

# 按文件大小：自动挑分辨率和色数，保证不超过 3 MB
python svg2gif.py fig.svg fig.gif --max-size 3MB

# 组合：优先从 1600 宽开始试，20 fps，224 色
python svg2gif.py fig.svg fig.gif --max-size 3MB --width 1600 --fps 20 --colors 224

# 白底（默认是透明背景）
python svg2gif.py fig.svg fig.gif --bg "#ffffff"

# 导出后自检
python svg2gif.py fig.svg fig.gif --width 800 --verify
```

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--width` / `--height` | 1200 | 输出像素尺寸，二选一；高度按 viewBox 比例算 |
| `--max-size` | 无 | 上限，如 `3MB`、`800KB`、`2500000`。会迭代缩放直到达标 |
| `--fps` | 20 | 帧率；帧数 = 周期 × fps |
| `--duration` | 自动 | 动画周期（秒）。默认取 SVG 里最大的 `dur` |
| `--colors` | 255 | 调色板颜色数。越小文件越小，但渐变会分层 |
| `--bg` | 透明 | 不透明背景色，如 `#ffffff` |
| `--loop` | 0 | 循环次数，0 = 无限 |
| `--still` | 否 | 只导出第一帧（静态 SVG 会自动这样） |
| `--trim` | 否 | 自动跳过片头淡入 / 片尾淡出的近乎空白段（见下） |
| `--crossfade` | 0.4 s | 循环接缝处的交叉淡化时长，0 = 关闭 |
| `--verify` | 否 | 导出后把 GIF 解码回来和编码前的帧逐帧比对 |
| `--keep-frames` | 否 | 保留临时截图，便于排查 |

## 它做了什么

1. **取帧**：动画没法直接读进 PIL，所以用 Chromium 打开它，把时间轴停住再逐帧定位：

   - SMIL 用 `svg.pauseAnimations()` + `svg.setCurrentTime(t)`；
   - CSS 动画走 Web Animations API：`document.getAnimations()` 逐个 `pause()`，
     每帧设 `currentTime = t * 1000`。

   两套时间轴必须**同时**驱动 —— 只停 SMIL 的话，CSS 那部分会按真实时间自己跑，
   转出来的 GIF 会随机闪。透明背景靠截图的 `omit_background=True`。
2. **调色板**：所有帧共用一张全局调色板。median cut 几乎不给画面里那几个很小的
   高饱和色（比如金属原子球）分配槽位，所以额外预留 24 个槽，用"出现频次 + 最小
   色距"从画面里真实存在的鲜艳色中挑出来填进去，否则绿色会被映射成青灰。
3. **编码**：自己拼 GIF89a 字节流，因为 Pillow 的透明动画优化不可用（见下）。

## 三个坑（也是这个工具存在的原因）

**1. 透明 GIF 的帧间差分不能照抄。**
GIF 的透明像素不是"画成透明"，而是**根本不绘制**。所以任何消失的元素（淡出的辉光、
脱去的原子）都不会被擦掉，会永久留在画面上成为鬼影。这里的处理是：某一步如果有
像素要消失，就先插一帧 `disposal=2`（恢复背景）**只清掉消失的那块**，紧接着再让
正常帧把该区域整块补画回来。两块都很小，几乎不占体积。

**2. `optimize=True` 会让颜色整体错位。**
Pillow 给每张小裁剪图单独编码时，默认会压缩调色板并**重映射索引**（一个 60×60 的
裁剪可能只写 4 色全局表）。而拼接时用的是统一的全局色表，索引对不上 → 每个差分帧
颜色全乱，表现为满屏闪烁，而整幅重画的帧恰好正常。所以必须 `optimize=False`。
（另一个后果：自检不能只看透明通道，必须同时比颜色。）

**3. 帧延时是厘秒整数。**
GIF 的延时单位是 1/100 秒。按毫秒四舍五入会累计误差（实测 120 帧丢了 0.36 s），
所以延迟按累计时间取整分配。

## 输出自检

`--verify` 会把 GIF 解码回 RGBA，按"动画步 → GIF 帧号"的映射和原始截图逐帧比较：

```
校验  : 152 帧, 总时长 6.00s, 最大透明差 0 px, 最大色差 7436 px
```

- **最大透明差**应为 0（说明没有鬼影）。
- **最大色差**是调色板量化误差，通常 1% 左右；想更准就加 `--colors`。
- **GIF 帧数 > 动画步数**是正常的：差值就是那些"先清屏再补画"的拆帧。

## 指定文件大小时怎么压

先说明单位：本工具里 **1 MB = 1 000 000 字节**（十进制）。这样"≤3MB"不管按哪种
口径理解都不会超 —— 3 MiB 是 3 145 728 字节，比它更严格。

GIF 体积近似正比于"像素数 × 帧数"，所以按下面的顺序让：

1. 先按 `--width`（默认 1200）编一次，超了就按 `sqrt(上限/实际)` 缩宽度重编；
2. 宽度缩到 420 px 还超标，就**帧率减半**（隔帧取样，时长不变）；
3. 还超标再缩宽度；
4. 缩到底（240 px）仍超标才降色数；
5. 最后如果离上限还有余量（< 75%），再把分辨率放回去，最多放大三次。

之所以先拿帧率换而不是色数：降色数会让原子球和渐变明显变色，而 20 → 10 fps
对这种机制图几乎看不出来。

## 片头片尾的"模糊闪屏"

很多 SVG 是"从全透明淡入、到接近空白淡出"的：第 0 帧几乎是空白，1~2 秒后才成形。
这在浏览器里是好看的，但做成循环 GIF 后，**每 12 秒（或文件自身的周期）都会先糊一下
再变清晰**，看起来就像闪屏。

`--trim` 会按"画面亮度标准差"找出画面真正成形的区间，把两头近乎空白的部分切掉；
`--crossfade`（默认 0.4 s）再把切完的结尾和开头做交叉淡化，接缝处不会硬跳。

```bash
python svg2gif.py fig.svg out.gif --max-size 3MB --trim
```

GUI 里对应"跳过片头淡入/片尾淡出"和"循环交叉淡化"两个控件，默认开启。

注意：如果淡入本身就是叙事的一部分（比如"从零开始搭建骨架"），别开这个选项 ——
它会把搭建过程的开头切掉。

## 周期是怎么定的

取所有动画里最长的那个时长（SMIL 的 `dur` 和 CSS 的 `animation` 时长一起比）。
只有当比它短得多（< 1/4）的装饰性动画除不尽时，才尝试 2×、3× 的倍数。

之前用"全体最小公倍数"的写法，被一个 0.55 s 的小动画把 12 s 的周期推到了 132 s，
渲染 792 帧才发现 —— 所以现在只让"显著"的动画参与整除判断。

## 跨平台

| 组件 | Windows | macOS | Linux |
|---|---|---|---|
| 命令行 | ✅ | ✅ | ✅ |
| 图形界面 | ✅ 自带 tkinter | ✅ 自带 tkinter | 需 `python3-tk` |
| Chromium | 自动下载 | 自动下载 | 自动下载（容器里可能要 `--no-sandbox`） |
| 字体 | 微软雅黑 | 苹方 | Noto / 文泉驿（都没有则退回 Tk 默认） |

代码里没有 Windows 专有调用：打开输出文件夹在三个平台上分别用 `os.startfile` /
`open` / `xdg-open`；路径全部走 `pathlib`；临时目录走 `tempfile`。

## 许可

**PolyForm Noncommercial License 1.0.0** —— 允许个人使用、研究、教学、非营利组织
和公共机构使用，允许修改和分发；**不允许商业用途**。商用需另行取得授权。

注意：这个协议**不是 OSI 意义上的开源协议**（OSI 的定义要求允许商业使用），
准确说法是 "source-available"。GitHub 的许可证识别可能把它显示为 "Other"。

如果你更想要一个学术圈更眼熟的协议，可以换成 CC BY-NC-SA 4.0；但 Creative Commons
官方不建议把 CC 协议用于软件。

## 限制

- 只处理 SMIL 和 CSS 动画；JS 驱动的动画（gsap、Canvas、WebGL）抓不到。
- GIF 只有 1 bit 透明度，半透明边缘会变成硬边；需要真正的 alpha 请考虑 APNG。
- 周期不是自动"感知"的，是按上面那条规则推的。动画本身不循环、或各元素周期互不
  整除时，用 `--duration` 手动指定。
- 渲染依赖 Chromium，无 GPU 的机器上大尺寸 + 高帧数会慢一些（约 70–100 ms/帧）。
