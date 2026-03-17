import os
from pathlib import Path
import json
import re
from contextlib import suppress
from typing import Union, Dict, List, Tuple, TYPE_CHECKING
from collections import defaultdict, deque
from .flat import *
from ..translate_main import PathConfig, FilePathConfig

EMPTY_TEXT = ['', '-']
AVOID_PATH = ['usage', 'id', 'model']

def getDataKey(key: tuple) -> str:
    return '.'.join([i if isinstance(i, str) else 'num' for i in key])

class ACAutomaton:
    """AC自动机，用于多模式字符串匹配"""
    class Node:
        __slots__ = ('children', 'fail', 'output')
        def __init__(self):
            self.children = {}      # 字符 -> 子节点
            self.fail = None         # 失败指针
            self.output = []         # 当前节点匹配的模式ID列表

    def __init__(self):
        self.root = self.Node()

    def add(self, word: str, pattern_id: int) -> None:
        """插入一个模式，pattern_id 为模式对应的编号"""
        node = self.root
        for ch in word:
            if ch not in node.children:
                node.children[ch] = self.Node()
            node = node.children[ch]
        node.output.append(pattern_id)

    def build(self) -> None:
        """构建失败指针，并合并输出"""
        q = deque()
        # 第一层节点的失败指针指向根
        for child in self.root.children.values():
            child.fail = self.root
            q.append(child)

        while q:
            r = q.popleft()
            for ch, child in r.children.items():
                q.append(child)
                # 寻找失败指针
                f = r.fail
                while f and ch not in f.children:
                    f = f.fail
                child.fail = f.children[ch] if f and ch in f.children else self.root
                # 合并失败节点的输出到当前节点
                if child.fail:
                    child.output.extend(child.fail.output)

    def search(self, text: str) -> set:
        """在文本中查找所有匹配的模式ID，返回去重后的集合"""
        node = self.root
        matched = set()
        for ch in text:
            # 沿失败指针转移，直到找到可匹配的边或根
            while node and ch not in node.children:
                node = node.fail
            if node and ch in node.children:
                node = node.children[ch]
            else:
                node = self.root
            # 收集当前节点所有匹配的模式ID
            if node.output:
                matched.update(node.output)
        return matched


class FileAnalyzer():
    def __init__(self, filePathConfig: FilePathConfig):
        self.filePath = filePathConfig
        self.load()

    def load(self):
        with open(self.filePath.KR_path, 'r', encoding='utf-8-sig') as f:
            self.text = f.read()
        try:
            self.data = json.loads(self.text)
        except:
            self.data = {}
        try:
            with open(self.filePath.LLC_path, 'r', encoding='utf-8-sig') as f:
                self.cnData = json.load(f)
        except:
            self.cnData = {}
        
        self._flat = flatten_dict_enhanced(self.data, ignore_types=[int, float])
        self._flat = {i: self._flat[i] for i in self._flat
                       if not (self._flat[i] in EMPTY_TEXT or i[-1] in AVOID_PATH)}
        
        self._cnFlat = flatten_dict_enhanced(self.cnData, ignore_types=[int, float])
        self._cnFlat = {i: self._cnFlat[i] for i in self._cnFlat
                       if not (self._cnFlat[i] in EMPTY_TEXT or i[-1] in AVOID_PATH)}
        
    # 原 analyze 方法已不再使用，保留空方法以避免外部调用出错（可选）
    def analyze(self, word: dict) -> Dict[str, Dict[str, int]]:
        return {}


class FileClassify():
    def __init__(self, rules: dict):
        self.rules = rules
        self._rules = {key: re.compile(value) for key, value in rules.items()}
    
    def classify(self, path: Union[str, Path]) -> str:
        path = str(path)
        for typeId, typeRe in self._rules.items():
            if typeRe.search(path):
                return typeId
        

class ProperAnalyzeMain():
    def __init__(self, pathConfig: PathConfig, hasPrefix: bool, classifyRules: dict,
                 minHit: float = 0.8, maxMiss: float = 0.0, divideZero: bool = True):
        self.pathConfig = pathConfig
        self.hasPrefix = hasPrefix
        self.fileClassify = FileClassify(classifyRules)
        self.data: Dict[str, Dict[str, FileAnalyzer]] = dict()
        self.result = dict()
        self.index = dict()
        self.minHit = minHit
        self.maxMiss = maxMiss
        self.devideZero = divideZero
        self.data = {i: dict() for i in classifyRules}
    
    def load(self):
        for path in self.pathConfig.KR_base_path.rglob('*.json'):
            filePathConfig = FilePathConfig(path, self.pathConfig, self.hasPrefix)
            fileType = self.fileClassify.classify(filePathConfig.rel_path)
            self.data[fileType][str(filePathConfig.rel_path)] = FileAnalyzer(filePathConfig)
    
    def analyze(self, word: dict):
        # 原 analyze 方法不再使用，但保留以兼容外部调用（可删除）
        return {}

    def checkOK(self, statistics: Dict[str, int]):
        _len = statistics.get('len', 0)
        _all = statistics.get('all', 0)
        _fit = statistics.get('fit', 0)
        if _all == 0:
            return self.devideZero
        return _fit/_all >= self.minHit and _all/_len <= self.maxMiss
    
    def preprocess(self, result: Dict[str, Dict[str, Dict[str, int]]]) -> Dict[str, Dict[str, bool]]:
        for fileType in result:
            result[fileType] = {key: self.checkOK(item) for key, item in result[fileType].items()}
        return result
    
    def makeIndex(self):
        for word in self.result:
            for fileType in self.result[word]:
                if fileType not in self.index:
                    self.index[fileType] = {}
                for key in self.result[word][fileType]:
                    if key not in self.index[fileType]:
                        self.index[fileType][key] = {}
                    self.index[fileType][key][word] = self.result[word][fileType][key]

    def init(self, words: List[dict]):
        """使用AC自动机一次性统计所有词条"""
        self.load()

        # 构建AC自动机
        term_ac = ACAutomaton()
        trans_ac = ACAutomaton()
        for idx, w in enumerate(words):
            term_ac.add(w['term'], idx)
            trans_ac.add(w['translation'], idx)
        term_ac.build()
        trans_ac.build()

        # 初始化统计容器
        # all_count[word_idx][fileType][dataKey] = 该词条term出现的路径数
        all_count = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        # fit_count[word_idx][fileType][dataKey] = 该词条term和translation同时出现的路径数
        fit_count = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        # len_by_key[fileType][dataKey] = 该dataKey在所有文件中出现的总次数
        len_by_key = defaultdict(lambda: defaultdict(int))

        # 遍历所有文件
        for fileType, files in self.data.items():
            for file_path, analyzer in files.items():
                kr_flat = analyzer._flat
                cn_flat = analyzer._cnFlat
                for path, kr_value in kr_flat.items():
                    dataKey = getDataKey(path)
                    len_by_key[fileType][dataKey] += 1

                    # 匹配韩文值中的term
                    term_matched = term_ac.search(kr_value)  # set of word indices

                    # 获取中文值并匹配translation
                    cn_value = cn_flat.get(path)
                    trans_matched = set()
                    if cn_value is not None:
                        trans_matched = trans_ac.search(cn_value)

                    # 更新统计
                    for word_idx in term_matched:
                        all_count[word_idx][fileType][dataKey] += 1
                        if word_idx in trans_matched:
                            fit_count[word_idx][fileType][dataKey] += 1

        # 构建 self.result，保持与原有格式一致
        self.result = {}
        for idx, word in enumerate(words):
            term = word['term']
            file_dict = {}
            for fileType in all_count[idx]:
                key_dict = {}
                for dataKey, all_val in all_count[idx][fileType].items():
                    fit_val = fit_count[idx][fileType].get(dataKey, 0)
                    len_val = len_by_key[fileType][dataKey]
                    key_dict[dataKey] = {'len': len_val, 'all': all_val, 'fit': fit_val}
                if key_dict:
                    file_dict[fileType] = key_dict
            if file_dict:
                self.result[term] = file_dict
            print(f'处理完成: {word}')

        self.makeIndex()
    
    def process(self, propers: List[str], fileType: str, keyPath: str) -> List[str]:
        return [i for i in propers if self.index[fileType][keyPath][i]]