from os.path import exists, join, basename, dirname, getmtime
from configparser import ConfigParser
from pandas import read_excel
import pandas as pd
import numpy as np


class Reader(object):
    """ These calls allow us to load excel files for building the SEP
    """

    def __init__(self, location, start_in=None, sheet=None,
                 cols=None):
        """
        :param location: path where main files are located
        :param start_in: this is a value which define where begin
        the data, (ignoring heading).
        :param sheet: str: which define the name of the sheet in
        the excel file
        :param cols: define the used cols

        """
        self.container = read_excel(self.verify(location),
                                    sheet_name=sheet,
                                    usecols=cols)
        self.start_in = start_in

    def __getitem__(self, sheet):
        """ Get the sheet from ordered dict generated from
        pandas read_excel

        :param sheet: str name
        :return: DataFrame
        """

        data = self.container.get(sheet)
        if data is not None:
            return data.iloc[self.start_in:, ]
        raise KeyError(f'{sheet} there is not in template '
                       f'expansion model')

    @staticmethod
    def verify(location):
        """ verify the existence of file location

        :param location: str
        :return: location
        """
        if exists(location):
            return location
        raise IOError(f'{location} Not Found!')


class Series(Reader):
    """ For managing time_series series. This class transforms series to npy files,
    so that loading is faster when running the program.

    init variable define index where time_series series continuous
    values begin.

    """

    def __init__(self, location, extra='simple', save_sheets=['MarginalCost', 'ExtractionGoal', 'Shifts', 'NodeAssignment', 'StationAssignment', 'GenProfiles']):
        """
        :param location: path where main files are located
        :param save_sheets: list of sheet names to save as .npy. If None, saves all sheets.
                           Example: ['MarginalCost', 'Emissions']

        """
        self.init = dict(MarginalCost=3, Emissions=3, ExtractionGoal=3, Shifts=6, NodeAssignment=5, StationAssignment=3, GenProfiles=3)
        self.save_sheets = save_sheets  # None = guardar todos, o lista de nombres
        self.conf, self.parser = 'series.ini', ConfigParser()
        self.dirname = dirname(location)
        self.meta_name = self.get_meta_name(location)

        filename, save = self.filename(location, extra)
        super().__init__(filename)
        self.load_and_save_elements(filename, extra, save)

    def load_and_save_elements(self, locate, extra, save):
        """ define when a element needs to be created or restored

        :param locate: file location
        :param extra: define the extra name of series
        :param save: define if we are going to store
        :return: None
        """
        if save:
            self.store_elements(locate, extra)
        else:
            self.load_binary_files()

    def store_elements(self, locate, extra):
        """ Save in excel format sheet a previous and short
        definition of time_series series

        :param locate: path of output time_series series
        :param extra: first name of time_series seres
        """
        candidate = self.build_candidate(locate, extra)
        with pd.ExcelWriter(candidate) as writer:
            self.store_data(writer)

    def store_data(self, writer):
        """ Save each produced short dataset in file writer
        definition

        :param writer:
        :return:
        """
        for sheet in self.container.keys():
            init = self.init.get(sheet)
            if init is None:
                continue
            subset = self.container[sheet].iloc[:, :init]
            subset.to_excel(writer, sheet, index=False)
            # Guardar como .npy solo si está en save_sheets o si save_sheets es None
            if self.save_sheets is None or sheet in self.save_sheets:
                self.store_series(sheet)

    def store_series(self, sheet):
        """
        :param sheet: name of the sheet
        :return:
        """
        init = self.init.get(sheet)
        series = self.container[sheet].iloc[:, init:]
        np.save(join(self.dirname, sheet), series.values)

    def load_binary_files(self):
        """ load files npy
        :return:
        """
        for sheet in self.container.keys():
            self.load_series(sheet)

    def load_series(self, sheet, ext='npy'):
        """ load specific npy based on simple restore values
        :param sheet:
        :param ext:
        :return:
        """
        name = join(self.dirname, f'{sheet}.{ext}')
        data = np.load(name, allow_pickle=True)
        columns = list(range(1, data.shape[1] + 1))
        values = (self.container[sheet],
                  pd.DataFrame(data, columns=columns))
        self.container[sheet] = pd.concat(values, axis=1)

    def build_candidate(self, location, extra):
        """ define simple version of serie.
        :param location:
        :param extra:
        :return:
        """

        file_name = f'{extra}_{basename(location)}'
        return join(self.dirname, file_name)

    def filename(self, location, extra):
        """ Select the location and filename in order to
        load the complete or simplified time_series series file

        :param location: original location file
        :param extra: extra named added to the minimal

        :return: a tuple with defined location and status
        """
        candidate = self.build_candidate(location, extra)
        # Verificar que el archivo simplificado existe Y todos los .npy existen
        if self.check_same_file(location) and exists(candidate) and self._all_npy_exist():
            return candidate, False
        self.update_state(str(getmtime(location)))
        return location, True

    def _all_npy_exist(self):
        """ Verifica que todos los archivos .npy esperados existen """
        for sheet in self.init.keys():
            npy_path = join(self.dirname, f'{sheet}.npy')
            if not exists(npy_path):
                return False
        return True

    def check_same_file(self, location):
        self.parser.read(join(self.dirname, self.conf))
        if 'meta' in self.parser:
            has_option = self.parser.has_option('meta', self.meta_name)
            if has_option:
                value = self.parser.get('meta', self.meta_name)
                if float(value) == getmtime(location):
                    return True
                return False
            else:
                self.create_config_file()
                return False
        else:
            self.create_config_file()
            return False

    def create_config_file(self):
        file = open(join(self.dirname, self.conf), 'w')
        if not 'meta' in self.parser:
            self.parser.add_section('meta')

        self.parser.set('meta', self.meta_name, '0')
        self.parser.write(file)
        file.close()

    def update_state(self, value):
        self.parser.read(join(self.dirname, self.conf))
        file = open(join(self.dirname, self.conf), 'w')
        self.parser.set('meta', self.meta_name, value)
        self.parser.write(file)
        file.close()

    def get_excel_name(self, location):
        end = location.find('.xlsx')
        start = location.rfind('\\') + 1
        name = location[start:end]
        return name

    def get_meta_name(self, location):
        excel_name = self.get_excel_name(location)
        base_name = 'time_creation'
        meta_name = f'{base_name}_{excel_name}'
        return meta_name


class Setting(object):
    """ Define the selected behaviour of selected
    study case, defining index and getter process.

    """

    def __init__(self, data, case_name='Case1'):
        """

        :param data: study case dataframe
        :param case_name: selected study case
        """
        self.data = data.set_index('name').loc[case_name]

    def __getitem__(self, item):
        """

        :param item:
        :return:
        """

        return self.data.get(item)
