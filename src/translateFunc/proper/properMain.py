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
    return '.'.join([i if isinstance(key, str) else 'num' for i in key])

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
        
    def analyze(self, word: dict) -> dict:
        '''
        {
            (keys, ): {
                'all': '',
                'fit': ''
            }
        }
        '''
        result = {}
        kr = word['term']
        cn = word['translation']
        for path, value in self._flat.items():
            if kr in value:
                dataKey = getDataKey(path)
                result[dataKey]['all'] = result.get(dataKey, {}).get('all', 0)+1
                with suppress(Exception):
                    if cn in self._cnFlat[path]:
                        result[dataKey]['fit'] = result.get(dataKey, {}).get('all', 0)+1
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
    def __init__(self, pathConfig: PathConfig, hasPrefix: bool, classifyRules: dict):
        self.pathConfig = pathConfig
        self.hasPrefix = hasPrefix
        self.fileClassify = FileClassify(classifyRules)
        self.data = dict()
        self.result = dict()
    
    def load(self):
        for path in self.pathConfig.KR_base_path.rglob('*.json'):
            filePathConfig = FilePathConfig(path, self.pathConfig, self.hasPrefix)
            fileType = self.fileClassify.classify(filePathConfig.rel_path)
            self.data[fileType][filePathConfig.KR_path] = FileAnalyzer(filePathConfig)
    
    def analyze(self, word: dict):
        for fileType in self.data:
            for file in self.data[fileType]:
                filePathConfig = FilePathConfig(file, self.pathConfig, self.hasPrefix)
