import os
from pathlib import Path
import json
import re
from contextlib import suppress
from typing import Union, Dict, List, Tuple, TYPE_CHECKING
from .flat import *
from ..translate_main import PathConfig, FilePathConfig

EMPTY_TEXT = ['', '-']
AVOID_PATH = ['usage', 'id', 'model']

def getDataKey(key: tuple) -> str:
    return '.'.join([i if isinstance(i, str) else 'num' for i in key])

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
        
    def analyze(self, word: dict) -> Dict[str, Dict[str, int]]:
        '''
        {
            'key.num': {
                'all': 1,
                'fit': 2,
                'len': 3
            }
        }
        '''
        result = {}
        kr = word['term']
        cn = word['translation']
        for path, value in self._flat.items():
            dataKey = getDataKey(path)
            # 确保 result[dataKey] 存在
            entry = result.setdefault(dataKey, {'len': 0, 'all': 0, 'fit': 0})
            entry['len'] += 1
            if kr in value:
                entry['all'] += 1
                with suppress(Exception):
                    if cn in self._cnFlat[path]:
                        entry['fit'] += 1
        return result

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
        result = {}
        for fileType in self.data:
            if fileType not in result:
                result[fileType] = {}
            for file in self.data[fileType]:
                analyzer = self.data[fileType][file]
                analyzeResult = analyzer.analyze(word)
                for key, item in analyzeResult.items():
                    if key not in result[fileType]:
                        result[fileType][key] = {}
                    for _key, _item in item.items():
                        result[fileType][key][_key] = result[fileType][key].get(_key, 0) + _item
        return result
    
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
        self.load()
        for word in words:
            result = self.analyze(word)
            result = self.preprocess(result)
            self.result[word['term']] = result
            print(f'处理完成: {word}')
        self.makeIndex()
    
    def process(self, propers: List[str], fileType: str, keyPath: str) -> List[str]:
        return [i for i in propers if self.index[fileType][keyPath][i]]