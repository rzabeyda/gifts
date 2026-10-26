"""Модерация чата: мат и ссылки (разрешена только ссылка на нашего бота). Чистые функции без обращения к Telegram."""
import re

# латиница, похожая на кириллицу: «xуй» -> «хуй»
_LOOK = str.maketrans({"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у", "k": "к", "m": "м", "t": "т", "h": "н", "b": "в", "ё": "е", "0": "о", "3": "з", "@": "а", "$": "с"})

# корни нецензурных слов (без двойных букв: повторы схлопываются заранее)
_RU = [
    r"хуй", r"хуя", r"хуе", r"хуи", r"хуев", r"хуйн", r"похуй", r"нахуй", r"нихуя", r"охуе", r"охуи", r"хули",
    r"пизд", r"пезд", r"пидор", r"пидар", r"пидр", r"педик", r"пидорас",
    r"ебан", r"ебат", r"ебал", r"ебну", r"ебуч", r"ебла", r"ебис", r"ебаш", r"заеб", r"уеб", r"выеб", r"поеб", r"разъеб", r"долбоеб", r"доеб", r"наеб", r"отъеб", r"еблан", r"ебло",
    r"бляд", r"блят", r"блять", r"бля", r"блеат",
    r"сука", r"суки", r"сучар", r"сучк",
    r"мудак", r"мудил", r"мудач", r"мудозвон",
    r"залуп", r"гандон", r"гондон", r"шлюх", r"шалав", r"манда", r"мандав", r"еблан", r"дрочи", r"дроч", r"охер", r"похер", r"нахер",
]
_EN = [r"fuck", r"fuk", r"shit", r"bitch", r"asshole", r"cunt", r"nigger", r"nigga", r"faggot", r"whore", r"dick"]
_RU_RX = re.compile(r"(?<![а-я])(?:" + "|".join(_RU) + r")")
_EN_RX = re.compile(r"(?<![a-z])(?:" + "|".join(_EN) + r")")
# слова, которые содержат корень, но матом не являются (нужны только для длинных корней, например «бля»)
_OK_WORDS = re.compile(r"\b(?:хули ган|сукр|хуила)\b")

_URL_RX = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/|tg://)\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|ru|org|net|io|me|tg|xyz|info|site|top|click|link|ua|su|co|app|gg|ly|cc|to|vip|shop|online|ton|bio|store|club)\b(?:/\S*)?", re.I)
_OURS = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/gift20bot(?:[/?#]|$)|^tg://resolve\?domain=gift20bot", re.I)


def _norm(s: str) -> str:
    s = s.lower().translate(_LOOK)
    s = re.sub(r"(.)\1+", r"\1", s)  # «хууууй» -> «хуй»
    return s


def has_profanity(text: str) -> bool:
    if not text:
        return False
    raw = text.lower()
    if _EN_RX.search(re.sub(r"(.)\1+", r"\1", raw)):
        return True
    n = _norm(text)
    n = _OK_WORDS.sub(" ", n)
    return bool(_RU_RX.search(n + " "))


def _is_ours(url: str) -> bool:
    return bool(_OURS.search(url.strip()))


def has_foreign_link(text: str, entities=None) -> bool:
    """True, если в сообщении есть ссылка, не ведущая на нашего бота."""
    text = text or ""
    for e in entities or []:
        t = getattr(e, "type", None) or (e.get("type") if isinstance(e, dict) else None)
        if t == "text_link":
            u = getattr(e, "url", None) or (e.get("url") if isinstance(e, dict) else "")
            if not _is_ours(u or ""):
                return True
    for m in _URL_RX.finditer(text):
        if not _is_ours(m.group(0)):
            return True
    return False


def has_any_link(text: str) -> bool:
    """Любая ссылка (в том числе на нашего бота): для комментариев к донатам."""
    return bool(_URL_RX.search(text or "")) or "@" in (text or "") and bool(re.search(r"@[A-Za-z0-9_]{4,}", text or ""))
