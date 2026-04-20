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

    model = Reader(join(args.data_folder, args.model), start_in=1)
    series = Series(join(args.data_folder, args.series))
    #time_series = timeseries.Timeseries(series, [1,91,181,271], 8/60)
    #time_series = timeseries.Timeseries(series, [1,32,60,91,121,152,182,213,244,274,305,335], 8/60) #12 dias significativos
    time_series = timeseries.Timeseries(series, [1], 8/60)
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
    parser.add_argument('--y_init_path', default=None,
                        help='Ruta opcional a Y.json para usar warm start en variable Y')

    args = parser.parse_args()
    series, mine_system, time_series = build_mine(args)
    gap= 1/100;
    solver_name=args.solver
    output_folder=args.output_folder
    y_init_path=args.y_init_path
    opt = OptimizationModel(mine_system, time_series, gap, solver_name, output_folder, y_init_path=y_init_path)


if __name__ == '__main__':
    main()
