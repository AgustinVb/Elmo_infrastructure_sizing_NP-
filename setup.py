from src import mine
from src.io.reader import Setting, Reader, Series
from src.time_series import timeseries
from src.optimization import OptimizationModel
import argparse, pprint
import pandas as pd
from os.path import join
import xlrd
xlrd.xlsx.ensure_elementtree_imported(False, None)
xlrd.xlsx.Element_has_iter = True





def build_mine(args):
    """ building power system base function

    :param args: a argument based list, you can access to any
    attribute using args.property.

    :return:
    """

    model = Reader(args.data_folder+args.model, start_in=1)
    series = Series(args.data_folder+args.series)
    #time_series = timeseries.Timeseries(series, [1,91,181,271], 0.5)
    time_series = timeseries.Timeseries(series, [1], 0.5)
    mine_system = mine.Mine(model)
    time_series.mapper['Trips'] = time_series.get_trips(mine_system)
    return series, mine_system, time_series


def main():
    """ Main function building argument collection from setting
    default values.

    :return:
    """
    system_title = 'ELMOMINE'
    parser = argparse.ArgumentParser(description=system_title)
    parser.add_argument('--data_folder', default='data/')
    parser.add_argument('--model', default='elmo_data.xlsx')
    parser.add_argument('--series', default='time_series.xlsx')
    parser.add_argument('--output_folder', default='output/')
    parser.add_argument('--solver', default='glpk')

    series, mine_system, time_series = build_mine(parser.parse_args())
    gap= 1/100;
    solver_name=parser.parse_args().solver
    output_folder=parser.parse_args().output_folder
    opt = OptimizationModel(mine_system, time_series, gap, solver_name, output_folder)


if __name__ == '__main__':
    main()
