"""Подборки: темы, в которые попадают модели из всех подарков сразу.

Модель попадает в тему, если её подарок указан в списке gifts (все модели подарка целиком)
или если название модели содержит одно из ключевых слов (целое слово, с учётом множественного числа).
"""
import re

# (id, название, ключевые слова в названии модели, подарки целиком)
THEMES = [
    ("pepe", "Pepe", "pepe*|frog*|toad*|amphibian|ribbit", ["Plush Pepe", "Kissed Frog"]),
    ("cats", "Коты", "cat|kitt*|neko|meow*|purr*|tabby|lynx|puma|panther|jaguar|cheetah|lion|tiger|floppa|leopard|gato|garfield|catacula|catnip|catwoman|ocelot|feline|whiskers|whisker|cheetah", ["Scared Cat", "Neko Helmet"]),
    ("dogs", "Собаки", "dog|doggy*|doge*|puppy|pup|hound|corgi|husky|pug|poodle|shiba|bulldog|dalmatian*|beagle|terrier|woof*|wolf|dogg*|doghouse|cheems|chihuahua|retriever|dalmatin|coyote|canine|canis|chow", ["Resistance Dog", "Snoop Dogg"]),
    ("ducks", "Утки", "duck*|quack*|utya*|utka|mallard|goose|gosling|donald|daffy|webbed", []),
    ("birds", "Птицы", "bird|parrot|owl|eagle|swan|crow|raven|phoenix|dove|pigeon|pigion|chicken|chick|chickadee|rooster|penguin|puffin|toucan|flamingo|peacock|falcon|hawk|heron|pelican|kakapo|jay|dodo|vulture|stork|sparrow|magpie|seagull|canary|bluebird|firebird|cockcrow|bullfinch|swift|cardinal", ["Rare Bird"]),
    ("bears", "Мишки", "bear|teddy|panda|koala|grizzly|winnie", ["Toy Bear"]),
    ("bunnies", "Зайчики", "bunn*|rabbit|hare", ["Jelly Bunny"]),
    ("monkeys", "Обезьяны", "monkey|chimp|gorilla|ape", ["Jolly Chimp"]),
    ("snakes", "Змеи", "snake*|cobra|python|serpent|viper", ["Pet Snake", "Snake Box", "Lunar Snake"]),
    ("dragons", "Драконы", "dragon*|wyvern|dino*|lizard|gecko|croc*|alligator", []),
    ("space", "Космос",
     "space|galaxy|cosmic|cosmos|astro|astronaut|moon|lunar|mars|planet|rocket|comet|alien|ufo|nebula|orbit|saturn|"
     "jupiter|venus|solar|meteor|stellar|starry|stardust|aurora|eclipse|satellite|mothership|supernova",
     ["Stellar Rocket"]),
    ("magic", "Магия и сказка", "unicorn|fairy|mermaid|wizard|witch|elf|magic|genie|spell|potion|enchanted|wand", []),
    ("halloween", "Хэллоуин",
     "pumpkin|ghost|skull|skeleton|vampire|zombie|frankenstein|spooky|halloween|monster|demon|devil|grave|coffin|"
     "reaper|haunt|spider|bat", ["Mad Pumpkin"]),
    ("newyear", "Новый год",
     "santa|snowman|christmas|xmas|reindeer|gingerbread|snowflake|sleigh|jingle|mitten|wreath|candy cane|holiday",
     ["Santa Hat", "Snow Mittens", "Sleigh Bell", "Snow Globe", "Xmas Stocking", "Jingle Bells", "Winter Wreath"]),
    ("sweets", "Сладости",
     "cake|cupcake|cookie|donut|pizza|burger|taco|sushi|ramen|ice cream|sundae|brownie|lollipop|gummy|pie|muffin|"
     "waffle|popcorn|chocolate|candy|cotton candy", ["Homemade Cake", "Ice Cream", "Vice Cream", "Happy Brownie"]),
    ("flowers", "Цветы", "flower|rose|tulip|sakura|daisy|lotus|blossom|orchid|lily|bouquet|posy|petal|clover|cactus",
     ["Sakura Flower", "Eternal Rose", "Lush Bouquet", "Pretty Posy"]),
    ("love", "Любовь", "love|heart|kiss|cupid|romance|valentine|crush|sweetheart",
     ["Heart Locket", "Trapped Heart", "Cupid Charm", "Valentine Box", "Love Candle", "Love Potion"]),
    ("heroes", "Воины и герои",
     "pirate|ninja|samurai|knight|viking|warrior|soldier|hero|gladiator|spartan|boxer|fighter|champion|sword|shield",
     ["Heroic Helmet", "Light Sword"]),
    ("tech", "Киберпанк", "robot|cyber|pixel|glitch|hacker|android|matrix|digital|crypto|laser|techno|punk", []),
    ("gold", "Золото", "gold|golden|gilded|bullion|ingot|nugget", []),
    ("cigars", "Сигары", "cigar*|cuban|havana|tobacco|humidor", ["Vintage Cigar", "Snoop Cigar"]),
    ("cars", "Автомобили",
     "car|taxi|bus|jeep|ferrari|lambo*|porsche|bmw|mustang|cadillac|racer|race car|drift|drifting|drifter|convertible|"
     "motorcycle|moto*|scooter|limo|limousine|hot rod|speedster|truck|tractor", ["Low Rider"]),
    ("adult", "18+",
     "naughty|kinky*|doggystyle|latex|lust beat|kamasutra|tip and strip|bikini*|eggplant*|aubergine*|"
     "spank*|adult|baywatch|peach*|uranus", ["Precious Peach"]),
    ("figures", "Статуэтки", "statue*|statuette*|figurine*|sculpture|bust|idol|trophy|oscar|monument|totem|gnome",
     ["Liberty Figure", "Mini Oscar"]),
    ("cakes", "Торты",
     "cake*|cupcake*|pastry|pastries|eclair|macaron*|tart*|tiramisu|cheesecake|muffin*|donut*|brownie*|pie|strudel|"
     "croissant|cannoli|profiterole|waffle*", ["Homemade Cake", "Mousse Cake", "Whip Cupcake"]),
    ("watches", "Часы", "watch|clock|chrono*|hourglass|timer", ["Swiss Watch"]),
    ("bags", "Сумки", "bag|handbag|purse|backpack|satchel|tote|clutch|pouch|sack", ["Loot Bag", "Swag Bag"]),
    ("rings", "Кольца", "ring|signet", ["Diamond Ring", "Signet Ring", "Gem Signet", "Bonded Ring"]),
    ("gems", "Камни", "diamond|crystal|jewel|gem|pearl|ruby|sapphire|emerald|treasure|crown",
     ["Diamond Ring", "Gem Signet", "Signet Ring", "Ion Gem"]),
    ("party", "Праздник", "party|disco|circus|clown|jester|birthday|confetti|carnival|balloon|festive|celebration",
     ["Party Sparkler", "Jester Hat", "B-Day Candle", "Jack-in-the-Box"]),
    ("pigeons", "Голуби", "pigeon|pigion|dove|columba", []),
    ("rodents", "Грызуны", "capybara|capy|hamster|mouse|mice|rat|squirrel|beaver|gerbil|chinchilla|guinea pig|rodent", []),
    ("pandas", "Панды", "panda", []),
    ("horses", "Лошади", "horse|pony|unicorn|stallion|mustang|zebra|donkey|gigahorse|pegasus|horseman", []),
    ("turtles", "Черепахи", "turtle|tortoise|donatello|leonardo|raphael|rafael|michelangelo|ninja mike", []),
    ("sea", "Море", "fish|shark|whale|dolphin|octopus|squid|crab|lobster|jellyfish|seahorse|sea horse|orca|seal|mermaid|goldfish|blobfish|devilfish|kraken|shrimp|salmon|starfish|clam|coral|koi|nemo|penguin|axolotl|piranha|walrus|otter", []),
    ("insects", "Насекомые", "bee|wasp|butterfly|ladybug|ladybird|spider|ant|beetle|moth|firefly|dragonfly|cricket|bug|bumble*|butterflight|butterflies|morpho|fireflies|hive|apiary|scarab|mantis|hornet", []),
    ("pigs", "Свинки", "pig|piggy|boar|hog", []),
    ("eggs", "Яйца",
     "egg|eggs|eggshell|eggsclusive|eggcellent|eggmoji|egghead|eggsecutive|eggburger|faberge|fabergé|easter|yolk|omelet|omelette",
     ["Easter Egg"]),
    ("glasses", "Очки",
     "glasses|sunglasses|goggles|aviator|aviators|spectacles|monocle|wayfarer|eyewear|specs|visor", ["Durov's Glasses"]),
    ("yellow", "Yellow", "yellow|lemon|banana|sunshine|sunny|mustard|canary", []),
    ("white", "White", "white|snow|ivory|pearl|alabaster|chalk|milk", []),
    ("platinum", "Platinum", "platinum|palladium|titanium|steel|iron|metal", []),
    ("orange", "Orange", "orange|tangerine|amber|apricot|mandarin", []),
    ("bronze", "Bronze", "bronze|copper|brass|rust", []),
    ("brown", "Brown", "brown|chocolate|coffee|mocha|cocoa|caramel", []),
    ("rainbow", "Rainbow", "rainbow|prism|iridescent|holographic|spectrum|kaleidoscope|opal|multicolor|pride", []),
    ("aqua", "Aqua", "aqua|turquoise|teal|cyan|azure|cerulean|lagoon", []),
    ("moon", "Луна",
     "moon|moons|moonlit|moonlight|moonstone|lunar|crescent|luna|selene|half moon|full moon|blood moon", ["Moon Pendant"]),
    # камни и цвета: по слову в названии модели
    ("ruby", "Ruby", "ruby", []),
    ("amethyst", "Amethyst", "amethyst|purple|violet|lilac|lavender|plum|grape|orchid", []),
    ("obsidian", "Obsidian", "obsidian", []),
    ("emerald", "Emerald",
     "emerald|aquamarine|jade|malachite|peridot|beryl|tourmaline|jasper|chrysoprase|aventurine|greenstone|moss agate|green|lime|olive|moss|forest", []),
    ("silver", "Silver", "silver", []),
    ("sapphire", "Sapphire", "sapphire|sapphires|blue|navy|cobalt|indigo|denim", []),
    ("topaz", "Topaz", "topaz", []),
    ("black", "Black", "black|jet black|matte black", []),
    ("chrome", "Chrome", "chrome|chromed|neo-chrome", []),
    ("rose", "Rose", "rose|roses|rosegold|pink|blush|bubblegum|fuchsia|magenta|barbie", []),
    ("diamond", "Diamond", "diamond|diamonds", []),
    ("red", "Red", "red", []),
]

# группы-фильтры вверху вкладки «Сэты»; всё, чего здесь нет, попадает в «other»
GROUPS = {
    "pets": ["pepe", "ducks", "cats", "dogs", "birds", "bears", "bunnies", "monkeys", "snakes", "dragons", "pigeons", "rodents", "pandas",
             "horses", "turtles", "sea", "insects", "pigs"],
    "gems": ["gold", "gems", "ruby", "amethyst", "obsidian", "emerald", "silver", "sapphire", "topaz", "black",
             "chrome", "rose", "diamond", "red", "yellow", "white", "platinum", "orange", "bronze", "brown", "rainbow", "aqua"],
    "status": ["bags", "rings", "glasses", "cars", "watches", "cigars"],
}
GROUP_OF = {tid: g for g, ids in GROUPS.items() for tid in ids}
# порядок на экране: блоки Питомцы, Статус, Цвет, затем остальное; внутри блока — как перечислено выше
_GORDER = {"pets": 0, "status": 1, "gems": 2}
_ORDER = {tid: (_GORDER[g], i) for g, ids in GROUPS.items() for i, tid in enumerate(ids)}
_ORDER["adult"] = (9, -1)  # 18+ первым среди остальных
THEMES.sort(key=lambda t: _ORDER.get(t[0], (9, 0)))

# отдельные модели, добавленные в тему вручную: тема -> {(подарок, модель)}
INCLUDE = {"pepe": {("Liberty Figure", "Plushie")}, "obsidian": {("Spring Basket", "Nocturne")}}



def _pattern(kw: str) -> "re.Pattern":
    """Слово со звёздочкой (duck*) совпадает с началом слова (Duckling, Duckie), без звёздочки — с целым словом."""
    parts = [re.escape(t[:-1]) + r"\w*" if t.endswith("*") else re.escape(t) + r"(?:s|es)?" for t in kw.split("|")]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.I)


_COMPILED = [(tid, title, _pattern(kw), set(gifts)) for tid, title, kw, gifts in THEMES]
TITLES = {tid: title for tid, title, _, _ in THEMES}
TITLES_EN = {
    "pepe": "Pepe", "ducks": "Ducks", "cats": "Cats", "dogs": "Dogs", "birds": "Birds", "bears": "Bears",
    "bunnies": "Bunnies", "monkeys": "Monkeys", "snakes": "Snakes", "dragons": "Dragons", "space": "Space",
    "magic": "Magic & Fairy Tales", "halloween": "Halloween", "newyear": "New Year", "sweets": "Sweets",
    "flowers": "Flowers", "love": "Love", "heroes": "Warriors & Heroes", "tech": "Cyberpunk",
    "gold": "Gold", "cigars": "Cigars", "cars": "Cars", "adult": "18+", "figures": "Figurines",
    "cakes": "Cakes", "watches": "Watches", "bags": "Bags", "rings": "Rings", "gems": "Gems", "party": "Party",
    "ruby": "Ruby", "amethyst": "Amethyst", "obsidian": "Obsidian", "emerald": "Emerald", "silver": "Silver",
    "sapphire": "Sapphire", "topaz": "Topaz", "black": "Black", "chrome": "Chrome", "rose": "Rose",
    "diamond": "Diamond", "red": "Red",
    "pigeons": "Pigeons", "rodents": "Rodents & Capybaras", "pandas": "Pandas", "horses": "Horses", "turtles": "Turtles",
    "sea": "Sea Life", "insects": "Insects", "pigs": "Pigs", "moon": "Moon", "yellow": "Yellow", "white": "White", "platinum": "Platinum", "orange": "Orange", "bronze": "Bronze",
    "brown": "Brown", "rainbow": "Rainbow", "aqua": "Aqua", "glasses": "Glasses", "eggs": "Eggs",
}


# сеты по внешнему виду: цвет картинки модели (visual_sets.json строится по превью, см. анализ цвета)
import json, pathlib
_VISUAL: dict[tuple[str, str], set[str]] = {}
try:
    for _tid, _ms in json.loads((pathlib.Path(__file__).parent / "visual_sets.json").read_text(encoding="utf-8")).items():
        for _g, _m in _ms:
            _VISUAL.setdefault((_g, _m), set()).add(_tid)
except (OSError, ValueError):
    pass


def match(gift: str, model: str) -> list[str]:
    """Темы, в которые попадает модель."""
    return [tid for tid, _, rx, gifts in _COMPILED
            if gift in gifts or tid in _VISUAL.get((gift, model), ()) or (gift, model) in INCLUDE.get(tid, ()) or (rx.search(model) and not (tid == "adult" and (gift, model) in ADULT_EXCLUDE))]


# модели, которые попадают в 18+ только из-за слова peach/bikini, но по смыслу не относятся: (подарок, модель)
ADULT_EXCLUDE = {
    ("Xmas Stocking", "Shiny Peach"), ("Tama Gadget", "Peach"), ("B-Day Candle", "Peachy"),
    ("Homemade Cake", "Tender Peach"), ("Snoop Dogg", "Peach Mode"), ("Santa Hat", "Peachy"),
    ("Big Year", "Peachy"), ("Pool Float", "Bikini Bottom"),
    ("Snow Mittens", "Aubergine"), ("Desk Calendar", "Peach Week"), ("Desk Calendar", "Plum Peach"),
    ("Sakura Flower", "Peach Perfect"), ("Love Potion", "Peach Nectar"), ("Homemade Cake", "Peachy Bloom"),
    ("Snow Globe", "Bikini Bottom"), ("Big Year", "Bikini Bottom"), ("Stellar Rocket", "Astro Peach"),
    ("Holiday Drink", "Peach Latte"), ("Hex Pot", "Peachy Fume"), ("Bow Tie", "Eggplants"),
}
